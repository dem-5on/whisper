"""Server-side lecture/meeting analysis over the operator's LLM key.

Transcripts (never raw audio, never user keys) go to the configured chat
model; structured stages come back parsed and validated. Generous failures
stay retryable: the daemon requeues them rather than losing the session.
"""

from __future__ import annotations

import json
from typing import Any


class AnalysisError(RuntimeError):
    """The analysis provider failed or returned an unusable result."""


class AnalysisModel:
    """Provider interface; returns raw model text plus the model used."""

    @property
    def models(self) -> tuple[str, ...]:
        raise NotImplementedError

    async def analyze(
        self, kind: str, text: str, *, language: str | None = None,
        model: str | None = None,
    ) -> tuple[str, str]:
        raise NotImplementedError


MAX_CHUNK_CHARS = 12_000
MAX_TOPICS = 20
MAX_TERMS = 30
MAX_QUESTIONS = 10
MAX_ACTIONS = 20
MAX_DECISIONS = 20


def split_transcript(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split a long transcript into wordsafe chunks for map-style stages."""
    words = text.split()
    chunks: list[str] = []
    current: list[str] = []
    current_chars = 0
    for word in words:
        if current and current_chars + len(word) + 1 > max_chars:
            chunks.append(" ".join(current))
            current = []
            current_chars = 0
        current.append(word)
        current_chars += len(word) + 1
    if current:
        chunks.append(" ".join(current))
    return [chunk for chunk in chunks if chunk.strip()]


def build_prompt(kind: str, text: str, *, language: str | None = None) -> tuple[str, str]:
    """Return (system, user) prompts for one analysis stage."""
    if kind == "notes":
        return (
            "Write structured study notes for this lecture transcript. Use short "
            "headings and bullet points. Preserve names, terms, numbers, and code "
            "identifiers exactly. Do not invent facts not present in the transcript. "
            "Return Markdown only.",
            text,
        )
    if kind == "topics":
        return (
            "Split this lecture transcript into topic sections. Return a JSON array "
            "and nothing else. Each entry has 'title' (short), 'summary' (one or two "
            "sentences), 'start_segment' and 'end_segment' (0-based indices into the "
            "numbered utterances below, or 0 when unsure). At most "
            f"{MAX_TOPICS} entries.",
            text,
        )
    if kind == "terms":
        return (
            "Extract key terms and definitions from this lecture transcript. Return "
            "a JSON array and nothing else. Each entry has 'term' and 'definition' "
            "(one sentence, grounded in the transcript). At most "
            f"{MAX_TERMS} entries.",
            text,
        )
    if kind == "questions":
        return (
            "Write review questions for this lecture transcript. Return a JSON "
            f"array of strings and nothing else. At most {MAX_QUESTIONS} questions.",
            text,
        )
    if kind == "translation":
        hint = f"The source language is '{language}'. " if language else ""
        return (
            "Translate this transcript into clear English. " + hint +
            "Preserve names, technical terms, and code identifiers; do not "
            "normalize them away. If the text is already English, return it "
            "unchanged. Return only the translated text.",
            text,
        )
    if kind == "summary":
        return (
            "Summarize this meeting transcript for someone who missed it: context, "
            "what was discussed, and outcomes. Preserve names, numbers, and "
            "decisions exactly. Return Markdown only.",
            text,
        )
    if kind == "actions":
        return (
            "Extract action items from this meeting transcript. Return a JSON "
            "array and nothing else. Each entry has 'action' (concrete task) and "
            "optionally 'owner' (a name from the transcript, otherwise empty). "
            f"At most {MAX_ACTIONS} entries.",
            text,
        )
    if kind == "decisions":
        return (
            "Extract the decisions made in this meeting transcript. Return a "
            "JSON array of strings and nothing else. Each entry states one "
            "decision plainly. Omit anything merely discussed without agreement. "
            f"At most {MAX_DECISIONS} entries.",
            text,
        )
    raise AnalysisError(f"Unknown analysis kind: {kind}")


def _strip_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2:
            cleaned = "\n".join(lines[1:])
            if cleaned.rstrip().endswith("```"):
                cleaned = cleaned.rstrip()[: -len("```")]
    return cleaned.strip()


def _as_list(payload: Any) -> list[Any]:
    return payload if isinstance(payload, list) else []


def parse_result(kind: str, text: str, *, segment_count: int | None = None) -> Any:
    """Validate one stage result; raises AnalysisError when unusable."""
    if kind in {"notes", "translation", "summary"}:
        cleaned = text.strip()
        if not cleaned:
            raise AnalysisError("The analysis provider returned an empty result")
        return cleaned
    try:
        payload = json.loads(_strip_fences(text))
    except ValueError as exc:
        raise AnalysisError("The analysis provider returned invalid JSON") from exc
    if kind == "decisions":
        decisions = []
        for entry in _as_list(payload)[:MAX_DECISIONS]:
            if isinstance(entry, dict):
                entry = entry.get("decision", "")
            decision = str(entry).strip()
            if decision:
                decisions.append(decision[:500])
        if not decisions:
            raise AnalysisError("The analysis provider returned no usable decisions")
        return decisions
    if kind == "topics":
        topics = []
        for entry in _as_list(payload)[:MAX_TOPICS]:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title", "")).strip()
            if not title:
                continue
            start = entry.get("start_segment", 0)
            end = entry.get("end_segment", start)
            try:
                start, end = int(start), int(end)
            except (TypeError, ValueError):
                start, end = 0, 0
            if start < 0 or end < start:
                start, end = 0, 0
            if segment_count is not None and (start >= segment_count or end >= segment_count):
                start, end = 0, 0
            topics.append({
                "title": title[:200],
                "summary": str(entry.get("summary", "")).strip()[:1000],
                "start_segment": start,
                "end_segment": end,
            })
        if not topics:
            raise AnalysisError("The analysis provider returned no usable topics")
        return topics
    if kind == "terms":
        terms = []
        for entry in _as_list(payload)[:MAX_TERMS]:
            if not isinstance(entry, dict):
                continue
            term = str(entry.get("term", "")).strip()
            definition = str(entry.get("definition", "")).strip()
            if term and definition:
                terms.append({"term": term[:200], "definition": definition[:1000]})
        if not terms:
            raise AnalysisError("The analysis provider returned no usable terms")
        return terms
    if kind == "questions":
        questions = []
        for entry in _as_list(payload)[:MAX_QUESTIONS]:
            if isinstance(entry, dict):
                entry = entry.get("question", "")
            question = str(entry).strip()
            if question:
                questions.append(question[:500])
        if not questions:
            raise AnalysisError("The analysis provider returned no usable questions")
        return questions
    if kind == "actions":
        actions = []
        for entry in _as_list(payload)[:MAX_ACTIONS]:
            if isinstance(entry, str):
                entry = {"action": entry}
            if not isinstance(entry, dict):
                continue
            action = str(entry.get("action", "")).strip()
            if not action:
                continue
            actions.append({
                "action": action[:500],
                "owner": str(entry.get("owner", "")).strip()[:200],
            })
        if not actions:
            raise AnalysisError("The analysis provider returned no usable actions")
        return actions
    raise AnalysisError(f"Unknown analysis kind: {kind}")


class OpenRouterAnalysisModel(AnalysisModel):
    """OpenRouter chat implementation; the operator key never leaves the host."""

    endpoint = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, models: tuple[str, ...], *, timeout_seconds: float = 60.0) -> None:
        if not api_key.strip():
            raise ValueError("OpenRouter API key is required")
        normalized = tuple(dict.fromkeys(item.strip() for item in models if item.strip()))
        if not normalized:
            raise ValueError("At least one OpenRouter model is required")
        if any(len(item) > 160 or any(ch.isspace() for ch in item) for item in normalized):
            raise ValueError("OpenRouter model IDs must be non-empty slugs")
        self._api_key = api_key.strip()
        self._models = normalized
        self.timeout_seconds = timeout_seconds

    @property
    def models(self) -> tuple[str, ...]:
        return self._models

    async def analyze(
        self, kind: str, text: str, *, language: str | None = None,
        model: str | None = None,
    ) -> tuple[str, str]:
        if kind not in ("notes", "topics", "terms", "questions", "translation", "summary", "actions", "decisions"):
            raise AnalysisError(f"Unknown analysis kind: {kind}")
        if not text.strip():
            raise AnalysisError("There is no transcript to analyze")
        selected = model or self._models[0]
        if selected not in self._models:
            raise AnalysisError("The requested analysis model is not enabled")
        try:
            from aiohttp import ClientSession, ClientTimeout
        except ImportError as exc:  # pragma: no cover - server extra is required in deployment
            raise AnalysisError("The analysis service is unavailable") from exc
        system, user = build_prompt(kind, text, language=language)
        payload = {
            "model": selected,
            "temperature": 0,
            "max_tokens": 4096,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            timeout = ClientTimeout(total=self.timeout_seconds)
            async with ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                    json=payload,
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise AnalysisError("The analysis provider did not complete the request")
                    result = await response.json(content_type=None)
        except AnalysisError:
            raise
        except Exception as exc:
            raise AnalysisError("The analysis provider is currently unavailable") from exc
        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AnalysisError("The analysis provider returned an invalid response") from exc
        if not isinstance(content, str) or not content.strip():
            raise AnalysisError("The analysis provider returned an empty response")
        return content, selected
