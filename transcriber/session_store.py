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
from contextlib import contextmanager
from typing import Any, Iterator


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
            with self._connection() as connection:
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

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Commit/rollback a connection transaction and always release its handle."""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

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
            with self._connection() as connection:
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
            with self._connection() as connection:
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

    def list(
        self, *, limit: int = 100, offset: int = 0, mode: str | None = None,
        search: str | None = None,
    ) -> list[StoredSession]:
        """List a stable page of sessions, newest first."""
        sessions, _has_more = self.list_page(limit=limit, offset=offset, mode=mode, search=search)
        return sessions

    def list_page(
        self, *, limit: int = 100, offset: int = 0, mode: str | None = None,
        search: str | None = None,
    ) -> tuple[list[StoredSession], bool]:
        """Return one history page and whether another page is available."""
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if mode is not None and mode not in SESSION_MODES:
            raise ValueError(f"mode must be one of: {', '.join(sorted(SESSION_MODES))}")
        if search is not None and (not isinstance(search, str) or len(search) > 200):
            raise ValueError("search must be text no longer than 200 characters")
        search = search.strip() if search else None
        clauses: list[str] = []
        values: list[Any] = []
        if mode is not None:
            clauses.append("mode = ?")
            values.append(mode)
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(source_transcript LIKE ? ESCAPE '\\' OR english_translation LIKE ? ESCAPE '\\')")
            values.extend((f"%{escaped}%", f"%{escaped}%"))
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        values.extend((limit + 1, offset))
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    f"SELECT id FROM sessions {where}ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                    values,
                ).fetchall()
        except sqlite3.Error as exc:
            raise SessionStoreError("Could not list local sessions") from exc
        has_more = len(rows) > limit
        return [self.get(row["id"]) for row in rows[:limit]], has_more

    def recover_interrupted(self) -> int:
        """Mark unfinished long-form sessions as interrupted without deleting audio.

        A daemon restart cannot safely resume microphone capture or assume a
        partially written WAV is complete. Keep the record and audio for later
        user review/recovery, but don't leave the session appearing active.
        """
        now = self._now()
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """UPDATE sessions
                       SET state = 'error',
                           error = 'Wicx closed before this session finished. The saved recording may be incomplete.',
                           updated_at = ?,
                           finished_at = COALESCE(finished_at, ?)
                       WHERE mode IN ('lecture', 'meeting')
                         AND state IN ('recording', 'transcribing', 'processing')""",
                    (now, now),
                )
                return cursor.rowcount
        except sqlite3.Error as exc:
            raise SessionStoreError("Could not recover interrupted local sessions") from exc

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
            with self._connection() as connection:
                cursor = connection.execute(
                    f"UPDATE sessions SET {', '.join(assignments)} WHERE id = ?", values
                )
                if cursor.rowcount == 0:
                    raise KeyError(session_id)
        except sqlite3.Error as exc:
            raise SessionStoreError("Could not update local session") from exc
        return self.get(session_id)

    def rename_speaker(self, session_id: str, speaker_id: str, display_name: str) -> StoredSession:
        """Persist a user-provided name for one diarization speaker ID."""
        if not isinstance(speaker_id, str) or not re.fullmatch(r"speaker_[1-9][0-9]*", speaker_id):
            raise ValueError("speaker_id must look like speaker_1")
        if not isinstance(display_name, str):
            raise ValueError("speaker name must be text")
        display_name = display_name.strip()
        if not display_name or len(display_name) > 80:
            raise ValueError("speaker name must be between 1 and 80 characters")
        if any(ord(char) < 32 or ord(char) == 127 for char in display_name):
            raise ValueError("speaker name cannot contain control characters")
        session = self.get(session_id)
        if session.mode != "meeting":
            raise ValueError("speaker names are only supported for Meeting sessions")
        speaker_names = session.outputs.get("speaker_names", {})
        if not isinstance(speaker_names, dict):
            speaker_names = {}
        speaker_names = {**speaker_names, speaker_id: display_name}
        return self.update(session_id, outputs={**session.outputs, "speaker_names": speaker_names})

    def delete(self, session_id: str) -> None:
        """Delete one session and its audio, never accepting an external file path."""
        record = self.get(session_id)
        session_dir = record.audio_path.parent
        try:
            # Remove the bytes before the index row. If filesystem deletion
            # fails, leave the session visible and retryable instead of
            # orphaning private audio with no history entry to find it.
            if session_dir.exists():
                shutil.rmtree(session_dir, ignore_errors=False)
            with self._connection() as connection:
                cursor = connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                if cursor.rowcount == 0:
                    raise KeyError(session_id)
        except (OSError, sqlite3.Error) as exc:
            raise SessionStoreError("Could not delete local session data") from exc

