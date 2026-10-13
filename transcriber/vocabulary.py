"""Persistent device-local custom vocabulary for speech-recognition hints."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import threading


MAX_TERMS = 500
MAX_TERM_LENGTH = 128
MAX_PROMPT_LENGTH = 800


class VocabularyError(RuntimeError):
    """A safe error while reading or changing the local dictionary."""


class VocabularyStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir).expanduser() / "vocabulary.json"
        self._lock = threading.RLock()

    def list(self) -> list[str]:
        with self._lock:
            return self._read()

    def add(self, raw_term: str) -> tuple[list[str], bool]:
        term = self._validate(raw_term)
        with self._lock:
            terms = self._read()
            if any(existing.casefold() == term.casefold() for existing in terms):
                return terms, False
            if len(terms) >= MAX_TERMS:
                raise VocabularyError(f"The dictionary is limited to {MAX_TERMS} terms")
            terms.append(term)
            self._write(terms)
            return terms, True

    def remove(self, raw_term: str) -> tuple[list[str], bool]:
        term = self._validate(raw_term)
        with self._lock:
            terms = self._read()
            remaining = [existing for existing in terms if existing.casefold() != term.casefold()]
            removed = len(remaining) != len(terms)
            if removed:
                self._write(remaining)
            return remaining, removed

    def initial_prompt(self) -> str | None:
        terms = self.list()
        if not terms:
            return None
        prefix = "Use these preferred spellings when they match what was said: "
        selected: list[str] = []
        current_length = len(prefix)
        for term in terms:
            addition = len(term) + (2 if selected else 0)
            if current_length + addition > MAX_PROMPT_LENGTH:
                break
            selected.append(term)
            current_length += addition
        return prefix + ", ".join(selected)

    @staticmethod
    def _validate(raw_term: str) -> str:
        if not isinstance(raw_term, str):
            raise VocabularyError("Dictionary terms must be text")
        term = raw_term.strip()
        if not term:
            raise VocabularyError("Dictionary term cannot be empty")
        if len(term) > MAX_TERM_LENGTH:
            raise VocabularyError(f"Dictionary terms must be {MAX_TERM_LENGTH} characters or fewer")
        if any(ord(char) < 32 or ord(char) == 127 for char in term):
            raise VocabularyError("Dictionary terms cannot contain control characters")
        return term

    def _read(self) -> list[str]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"terms": []}
        except (OSError, json.JSONDecodeError) as exc:
            raise VocabularyError("Could not read the local dictionary") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("terms"), list):
            raise VocabularyError("The local dictionary file is invalid")
        terms = payload["terms"]
        if len(terms) > MAX_TERMS or any(not isinstance(term, str) for term in terms):
            raise VocabularyError("The local dictionary file is invalid")
        return terms

    def _write(self, terms: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(6)}.tmp")
        try:
            temporary.write_text(json.dumps({"terms": terms}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if os.name != "nt":
                temporary.chmod(0o600)
            temporary.replace(self.path)
            if os.name != "nt":
                self.path.chmod(0o600)
        except OSError as exc:
            raise VocabularyError("Could not save the local dictionary") from exc
        finally:
            temporary.unlink(missing_ok=True)
