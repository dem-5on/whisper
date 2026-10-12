"""Modular text-rewrite providers used by paid cloud features."""

from __future__ import annotations

from typing import Protocol


class RewriteError(RuntimeError):
    """A rewrite provider could not return a usable result."""


class RewriteModel(Protocol):
    @property
    def models(self) -> tuple[str, ...]: ...

    async def rewrite(self, text: str, *, model: str | None = None) -> tuple[str, str]: ...


class OpenRouterRewriteModel:
    """OpenRouter implementation; secrets stay on the Wicx service host."""

    endpoint = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, models: tuple[str, ...], *, timeout_seconds: float = 12.0) -> None:
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

    async def rewrite(self, text: str, *, model: str | None = None) -> tuple[str, str]:
        selected = model or self._models[0]
        if selected not in self._models:
            raise RewriteError("The requested rewrite model is not enabled")
        try:
            from aiohttp import ClientSession, ClientTimeout
        except ImportError as exc:  # pragma: no cover - server extra is required in deployment
            raise RewriteError("The rewrite service is unavailable") from exc

        payload = {
            "model": selected,
            "temperature": 0,
            "max_tokens": 2048,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Clean up this speech transcript for direct insertion into the user's text field. "
                        "Correct punctuation, capitalization, grammar, and clear transcription errors; "
                        "remove non-meaningful filler and accidental repetition. Preserve the speaker's "
                        "meaning, names, technical terms, and any languages they used. Do not translate, "
                        "add facts, answer questions, or introduce commentary. Return only the cleaned text."
                    ),
                },
                {"role": "user", "content": text},
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
                        raise RewriteError("The rewrite provider did not complete the request")
                    result = await response.json(content_type=None)
        except RewriteError:
            raise
        except Exception as exc:
            raise RewriteError("The rewrite provider is currently unavailable") from exc

        try:
            rewritten = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RewriteError("The rewrite provider returned an invalid response") from exc
        if not isinstance(rewritten, str) or not rewritten.strip():
            raise RewriteError("The rewrite provider returned an empty response")
        return rewritten.strip(), selected
