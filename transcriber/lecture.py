"""Lecture post-processing input builders for hosted analysis stages.

Pure helpers (no network, no daemon state) so the chunking, numbering, and
merging rules are unit-testable. Transport and persistence live on the daemon.
"""

from __future__ import annotations

from typing import Any


LECTURE_STAGES: tuple[str, ...] = ("translation", "notes", "topics", "terms", "questions")

#: Analysis kind -> session outputs key holding the finished artifact.
OUTPUT_KEYS: dict[str, str] = {
    "notes": "lecture_notes",
    "topics": "topic_groups",
    "terms": "key_terms",
    "questions": "review_questions",
}

#: Stay comfortably under the service 60k-char request cap.
MAX_STAGE_CHARS = 45_000


def split_text(text: str, max_chars: int = MAX_STAGE_CHARS) -> list[str]:
    """Split wordsafely so each piece fits one analysis request."""
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


def numbered_utterances(segments: list[dict[str, Any]], transcript: str) -> tuple[str, int]:
    """Number utterances for topic index linkage; falls back to sentences."""
    lines: list[str] = []
    for index, segment in enumerate(segments or []):
        text = str(segment.get("text", "")).strip()
        if text:
            lines.append(f"{index}: {text}")
    if lines:
        return "\n".join(lines), len(lines)
    sentences = [part.strip() for part in transcript.replace("\n", " ").split(". ") if part.strip()]
    return "\n".join(f"{index}: {sentence}" for index, sentence in enumerate(sentences)), len(sentences)


def adjust_topics(topics: Any, offset: int, total: int) -> list[dict[str, Any]]:
    """Shift per-chunk topic indices to global segments and clamp."""
    adjusted: list[dict[str, Any]] = []
    if not isinstance(topics, list):
        return adjusted
    for entry in topics:
        if not isinstance(entry, dict):
            continue
        try:
            start = int(entry.get("start_segment", 0)) + offset
            end = int(entry.get("end_segment", start - offset)) + offset
        except (TypeError, ValueError):
            start, end = offset, offset
        if start < 0 or end < start or (total and (start >= total or end >= total)):
            start, end = offset, offset
        adjusted.append({
            "title": str(entry.get("title", "")).strip()[:200],
            "summary": str(entry.get("summary", "")).strip()[:1000],
            "start_segment": start,
            "end_segment": end,
        })
    return [entry for entry in adjusted if entry["title"]]
