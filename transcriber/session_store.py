"""Durable, device-local storage for Dictation, Lecture, and Meeting sessions.

Session metadata lives in SQLite; long-form audio lives in one directory per
session. This keeps audio out of the database while giving later sync/export
code stable records and file locations to work with.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import uuid
from typing import Any


SESSION_MODES = frozenset({"dictation", "lecture", "meeting"})
SESSION_STATES = frozenset({"recording", "transcribing", "processing", "ready", "error", "cancelled"})


class SessionStoreError(RuntimeError):
    """A safe, user-facing session storage error."""


@dataclass(frozen=True)
class StoredSession:
    id: str
    mode: str
    state: str
    created_at: str
    updated_at: str
    finished_at: str | None
    source_transcript: str
    english_translation: str
    outputs: dict[str, Any]
    error: str | None
    audio_path: Path


class SessionStore:
    """Persist session metadata and audio beneath one application data root."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).expanduser()
        self.sessions_dir = self.data_dir / "sessions"
        self.database = self.data_dir / "sessions.sqlite3"
        try:
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                self.sessions_dir.chmod(0o700)
            with self._connect() as connection:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        mode TEXT NOT NULL,
                        state TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        finished_at TEXT,
                        source_transcript TEXT NOT NULL DEFAULT '',
                        english_translation TEXT NOT NULL DEFAULT '',
                        outputs_json TEXT NOT NULL DEFAULT '{}',
                        error TEXT
                    )"""
                )
                connection.execute("CREATE INDEX IF NOT EXISTS sessions_created_at ON sessions(created_at)")
            if os.name != "nt":
                self.database.chmod(0o600)
        except (OSError, sqlite3.Error) as exc:
            raise SessionStoreError("Could not prepare local session storage") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="milliseconds")

    def create(self, mode: str) -> StoredSession:
        if mode not in SESSION_MODES:
            raise ValueError(f"mode must be one of: {', '.join(sorted(SESSION_MODES))}")
        session_id = uuid.uuid4().hex
        now = self._now()
        session_dir = self.sessions_dir / session_id
        try:
            session_dir.mkdir(mode=0o700)
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO sessions (id, mode, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (session_id, mode, "recording", now, now),
                )
        except (OSError, sqlite3.Error) as exc:
            shutil.rmtree(session_dir, ignore_errors=True)
            raise SessionStoreError("Could not create a local session") from exc
        return self.get(session_id)

    def audio_path(self, session_id: str) -> Path:
        """Return the fixed recording destination for an existing session."""
        record = self.get(session_id)
        path = record.audio_path
        if not path.parent.is_dir():
            raise SessionStoreError("Session audio folder is unavailable")
        return path

    def get(self, session_id: str) -> StoredSession:
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        except sqlite3.Error as exc:
            raise SessionStoreError("Could not read local session") from exc
        if row is None:
            raise KeyError(session_id)
        if not re.fullmatch(r"[0-9a-f]{32}", row["id"]):
            raise SessionStoreError("Session identifier is invalid")
        try:
            outputs = json.loads(row["outputs_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise SessionStoreError("Session output data is damaged") from exc
        if not isinstance(outputs, dict):
            raise SessionStoreError("Session output data is damaged")
        return StoredSession(
            id=row["id"], mode=row["mode"], state=row["state"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            finished_at=row["finished_at"], source_transcript=row["source_transcript"],
            english_translation=row["english_translation"], outputs=outputs,
            error=row["error"], audio_path=self.sessions_dir / row["id"] / "recording.wav",
        )

    def list(self, *, limit: int = 100, mode: str | None = None) -> list[StoredSession]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if mode is not None and mode not in SESSION_MODES:
            raise ValueError(f"mode must be one of: {', '.join(sorted(SESSION_MODES))}")
        try:
            with self._connect() as connection:
                if mode is None:
                    rows = connection.execute(
                        "SELECT id FROM sessions ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT id FROM sessions WHERE mode = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                        (mode, limit),
                    ).fetchall()
        except sqlite3.Error as exc:
            raise SessionStoreError("Could not list local sessions") from exc
        return [self.get(row["id"]) for row in rows]

    def update(
        self,
        session_id: str,
        *,
        state: str | None = None,
        source_transcript: str | None = None,
        english_translation: str | None = None,
        outputs: dict[str, Any] | None = None,
        error: str | None = None,
        finished: bool = False,
    ) -> StoredSession:
        if state is not None and state not in SESSION_STATES:
            raise ValueError(f"state must be one of: {', '.join(sorted(SESSION_STATES))}")
        if outputs is not None and not isinstance(outputs, dict):
            raise ValueError("outputs must be a mapping")
        assignments = ["updated_at = ?"]
        values: list[Any] = [self._now()]
        for field, value in (
            ("state", state),
            ("source_transcript", source_transcript),
            ("english_translation", english_translation),
            ("error", error),
        ):
            if value is not None:
                assignments.append(f"{field} = ?")
                values.append(value)
        if outputs is not None:
            try:
                encoded = json.dumps(outputs, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError) as exc:
                raise ValueError("outputs must contain JSON-compatible values") from exc
            assignments.append("outputs_json = ?")
            values.append(encoded)
        if finished:
            assignments.append("finished_at = ?")
            values.append(self._now())
        values.append(session_id)
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    f"UPDATE sessions SET {', '.join(assignments)} WHERE id = ?", values
                )
                if cursor.rowcount == 0:
                    raise KeyError(session_id)
        except sqlite3.Error as exc:
            raise SessionStoreError("Could not update local session") from exc
        return self.get(session_id)

    def delete(self, session_id: str) -> None:
        """Delete one session and its audio, never accepting an external file path."""
        record = self.get(session_id)
        session_dir = record.audio_path.parent
        try:
            with self._connect() as connection:
                cursor = connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                if cursor.rowcount == 0:
                    raise KeyError(session_id)
            shutil.rmtree(session_dir, ignore_errors=False)
        except (OSError, sqlite3.Error) as exc:
            raise SessionStoreError("Could not delete local session data") from exc

