"""Small server-owned reconciler for rolling-window speech hypotheses."""

from __future__ import annotations


def _common_prefix(left: list[str], right: list[str]) -> int:
    for index, (old, new) in enumerate(zip(left, right)):
        if old != new:
            return index
    return min(len(left), len(right))


def _suffix_prefix_overlap(previous: list[str], current: list[str]) -> int:
    for length in range(min(len(previous), len(current)), 0, -1):
        if previous[-length:] == current[:length]:
            return length
    return 0


class RollingTranscript:
    """Convert overlapping decoder windows into one full current hypothesis."""

    def __init__(self) -> None:
        self._full_words: list[str] = []
        self._previous_window: list[str] = []
        self._window_start = 0
        self._committed_count = 0

    def update(self, text: str) -> str:
        words = text.split()
        if not self._previous_window:
            self._full_words = list(words)
            self._previous_window = list(words)
            return " ".join(self._full_words)

        common = _common_prefix(self._previous_window, words)
        overlap = _suffix_prefix_overlap(self._previous_window, words)
        if common:
            stable_end = self._window_start + common
            replace_from = max(self._committed_count, stable_end)
            current_from = max(0, replace_from - self._window_start)
            self._full_words = self._full_words[:replace_from] + words[current_from:]
        elif overlap:
            next_start = self._window_start + len(self._previous_window) - overlap
            self._committed_count = max(self._committed_count, next_start)
            self._full_words += words[overlap:]
            self._window_start = next_start
        else:
            self._full_words = self._full_words[:self._committed_count] + words
            self._window_start = self._committed_count
        self._previous_window = list(words)
        if common:
            self._committed_count = max(self._committed_count, self._window_start + common)
        self._committed_count = min(self._committed_count, len(self._full_words))
        return " ".join(self._full_words)

    def clear(self) -> None:
        self._full_words.clear()
        self._previous_window.clear()
        self._window_start = 0
        self._committed_count = 0
