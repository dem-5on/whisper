"""Live-session state: stable-prefix reconciliation and rolling PCM window.

Whisper is not inherently streaming: re-decoding a sliding window yields
hypotheses whose tail words flicker. The panel therefore shows two parts:

* ``committed`` — the stable prefix across consecutive hypotheses. Only
  grows, never changes; safe to read.
* ``provisional`` — the newest words, still subject to revision.

Only the finalized transcript (after Stop / endpointing) is delivered to
the focused app. Live keyboard insertion would need delete-and-retype
correction loops that are fragile across Wayland apps, so it is not enabled.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field


def stable_prefix(previous: list[str], current: list[str]) -> list[str]:
    """Longest common word prefix between two consecutive hypotheses."""
    shared: list[str] = []
    for old, new in zip(previous, current):
        if old != new:
            break
        shared.append(old)
    return shared


def reconcile(previous_committed: list[str], previous_full: list[str], current: list[str]) -> tuple[list[str], list[str]]:
    """Advance (committed, provisional) given a fresh window hypothesis.

    The new committed text is the longest prefix of ``current`` that extends
    (or equals) the previously committed words *and* agrees with the
    previous full hypothesis on the overlapping region. In practice this is
    ``common_prefix(previous_full, current)`` floored at the old committed
    length so committed text never shrinks. Everything after it is
    provisional.
    """
    agreed = stable_prefix(previous_full, current)
    if len(agreed) < len(previous_committed):
        agreed = list(previous_committed)
    return agreed, current[len(agreed):]


def suffix_prefix_overlap(previous: list[str], current: list[str]) -> int:
    """Length of the longest suffix of ``previous`` matching ``current``.

    A rolling audio window eventually drops its earliest samples. Its next
    transcript consequently starts part-way through the prior hypothesis,
    rather than at word zero. Prefix-only reconciliation cannot represent
    that transition.
    """
    limit = min(len(previous), len(current))
    for length in range(limit, 0, -1):
        if previous[-length:] == current[:length]:
            return length
    return 0


@dataclass
class LiveEvent:
    type: str  # partial | committed | final | speech_started | speech_ended
    session_id: str
    revision: int
    text: str = ""
    at: float = field(default_factory=time.time)


class LiveSession:
    """Per-recording live state owned by the daemon."""

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id or uuid.uuid4().hex[:8]
        self.revision = 0
        self.committed_words: list[str] = []
        self.provisional_words: list[str] = []
        self._full_words: list[str] = []
        self._previous_window: list[str] = []
        # Word offset of ``_previous_window`` in ``_full_words``. It moves
        # forward whenever the PCM rolling window discards old audio.
        self._previous_window_start = 0
        self._committed_count = 0
        self.speech_active = False
        self.events: deque[LiveEvent] = deque(maxlen=200)

    # -- hypothesis updates -------------------------------------------
    def push_hypothesis(self, text: str) -> LiveEvent:
        words = text.split()
        old_committed = self._committed_count
        if not self._previous_window:
            self._full_words = list(words)
            self._previous_window = list(words)
            self._previous_window_start = 0
        else:
            common = len(stable_prefix(self._previous_window, words))
            overlap = suffix_prefix_overlap(self._previous_window, words)
            # Normal rolling decodes cover the same window start, so revise
            # only the provisional tail after the common prefix.
            if common:
                stable_end = self._previous_window_start + common
                replace_from = max(self._committed_count, stable_end)
                current_from = max(0, replace_from - self._previous_window_start)
                self._full_words = self._full_words[:replace_from] + words[current_from:]
            elif overlap:
                # The window advanced. Everything before its overlap has aged
                # out of the decoder window and is now safe to retain; append
                # only audio that is genuinely new after the overlap.
                next_start = self._previous_window_start + len(self._previous_window) - overlap
                self._committed_count = max(self._committed_count, next_start)
                self._full_words += words[overlap:]
                self._previous_window_start = next_start
            else:
                # A decoder revision with no usable alignment must never
                # discard committed text or slice words by a stale offset.
                self._full_words = self._full_words[:self._committed_count] + words
                self._previous_window_start = self._committed_count
            self._previous_window = list(words)

            # Consecutive same-start hypotheses make their shared prefix safe.
            if common:
                self._committed_count = max(
                    self._committed_count,
                    self._previous_window_start + common,
                )
        self._committed_count = min(self._committed_count, len(self._full_words))
        self.committed_words = self._full_words[:self._committed_count]
        self.provisional_words = self._full_words[self._committed_count:]
        grew = self._committed_count > old_committed
        self.revision += 1
        event = LiveEvent(
            type="committed" if grew else "partial",
            session_id=self.session_id,
            revision=self.revision,
            text=self.live_text,
        )
        self.events.append(event)
        return event

    def note_speech(self, started: bool) -> LiveEvent:
        self.speech_active = started
        self.revision += 1
        event = LiveEvent(
            type="speech_started" if started else "speech_ended",
            session_id=self.session_id,
            revision=self.revision,
        )
        self.events.append(event)
        return event

    def finalize(self, text: str) -> LiveEvent:
        words = text.split()
        # Final text supersedes provisional words entirely.
        self.committed_words = words
        self.provisional_words = []
        self._full_words = words
        self._previous_window = words
        self._previous_window_start = 0
        self._committed_count = len(words)
        self.speech_active = False
        self.revision += 1
        event = LiveEvent(
            type="final",
            session_id=self.session_id,
            revision=self.revision,
            text=text,
        )
        self.events.append(event)
        return event

    # -- views ----------------------------------------------------------
    @property
    def committed_text(self) -> str:
        return " ".join(self.committed_words)

    @property
    def provisional_text(self) -> str:
        return " ".join(self.provisional_words)

    @property
    def live_text(self) -> str:
        if self.committed_words and self.provisional_words:
            return f"{self.committed_text} {self.provisional_text}"
        return self.committed_text or self.provisional_text


class RollingBuffer:
    """Bounded PCM accumulator; exposes the most recent N seconds as samples."""

    def __init__(self, window_seconds: int = 15, sample_rate: int = 16_000) -> None:
        self.sample_rate = sample_rate
        self.capacity_bytes = int(window_seconds * sample_rate) * 2
        self._data = bytearray()

    def append(self, pcm: bytes) -> None:
        self._data += pcm
        if len(self._data) > self.capacity_bytes:
            del self._data[: len(self._data) - self.capacity_bytes]

    def window_bytes(self) -> bytes:
        return bytes(self._data)

    def window_samples(self):  # type: ignore[no-untyped-def]
        """Most recent window as float32 mono in [-1, 1]; empty array if none."""
        try:
            import numpy as np
        except ImportError:
            return None
        if not self._data:
            return np.zeros(0, dtype=np.float32)
        raw = np.frombuffer(bytes(self._data), dtype="<i2").astype(np.float32)
        return raw / 32768.0

    def duration_seconds(self) -> float:
        return len(self._data) / 2 / self.sample_rate

    def clear(self) -> None:
        self._data.clear()
