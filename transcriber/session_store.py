"""Durable, device-local storage for Dictation, Lecture, and Meeting sessions.

Session metadata lives in SQLite; long-form audio lives in one directory per
session. This keeps audio out of the database while giving later sync/export
code stable records and file locations to work with.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import math
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
MAX_TRANSCRIPT_SEGMENTS = 100_000


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
    segments: list[dict[str, Any]]
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
                        segments_json TEXT NOT NULL DEFAULT '[]',
                        english_translation TEXT NOT NULL DEFAULT '',
                        outputs_json TEXT NOT NULL DEFAULT '{}',
                        error TEXT
                    )"""
                )
                columns = {row["name"] for row in connection.execute("PRAGMA table_info(sessions)")}
                if "segments_json" not in columns:
                    connection.execute("ALTER TABLE sessions ADD COLUMN segments_json TEXT NOT NULL DEFAULT '[]'")
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
            segments = json.loads(row["segments_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise SessionStoreError("Session data is damaged") from exc
        if not isinstance(outputs, dict) or not isinstance(segments, list):
            raise SessionStoreError("Session data is damaged")
        try:
            segments = self._validate_segments(segments)
        except ValueError as exc:
            raise SessionStoreError("Session transcript segments are damaged") from exc
        return StoredSession(
            id=row["id"], mode=row["mode"], state=row["state"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            finished_at=row["finished_at"], source_transcript=row["source_transcript"], segments=segments,
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

    def recover_interrupted(self) -> list[str]:
        """Mark unfinished long-form sessions as interrupted without deleting audio.

        A daemon restart cannot safely resume microphone capture or assume a
        partially written WAV is complete. Keep the record and audio for later
        user review/recovery, but don't leave the session appearing active.
        Returns the affected session ids so callers can repair audio headers.
        """
        now = self._now()
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """SELECT id FROM sessions
                       WHERE mode IN ('lecture', 'meeting')
                         AND state IN ('recording', 'transcribing', 'processing')""",
                ).fetchall()
                ids = [row["id"] for row in rows]
                connection.execute(
                    """UPDATE sessions
                       SET state = 'error',
                           error = 'Wicx closed before this session finished. The saved recording may be incomplete.',
                           updated_at = ?,
                           finished_at = COALESCE(finished_at, ?)
                       WHERE mode IN ('lecture', 'meeting')
                         AND state IN ('recording', 'transcribing', 'processing')""",
                    (now, now),
                )
                return ids
        except sqlite3.Error as exc:
            raise SessionStoreError("Could not recover interrupted local sessions") from exc

    def update(
        self,
        session_id: str,
        *,
        state: str | None = None,
        source_transcript: str | None = None,
        segments: list[dict[str, Any]] | None = None,
        english_translation: str | None = None,
        outputs: dict[str, Any] | None = None,
        error: str | None = None,
        finished: bool = False,
    ) -> StoredSession:
        if state is not None and state not in SESSION_STATES:
            raise ValueError(f"state must be one of: {', '.join(sorted(SESSION_STATES))}")
        if outputs is not None and not isinstance(outputs, dict):
            raise ValueError("outputs must be a mapping")
        if segments is not None:
            segments = self._validate_segments(segments)
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
        if segments is not None:
            assignments.append("segments_json = ?")
            values.append(json.dumps(segments, ensure_ascii=False, separators=(",", ":")))
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

    @staticmethod
    def _validate_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(segments, list) or len(segments) > MAX_TRANSCRIPT_SEGMENTS:
            raise ValueError(f"segments must be a list of at most {MAX_TRANSCRIPT_SEGMENTS} entries")
        normalized: list[dict[str, Any]] = []
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                raise ValueError(f"segment {index} must be a mapping")
            start = segment.get("start_seconds")
            end = segment.get("end_seconds")
            text = segment.get("text")
            speaker_id = segment.get("speaker_id")
            if (isinstance(start, bool) or not isinstance(start, (int, float))
                    or isinstance(end, bool) or not isinstance(end, (int, float))
                    or not math.isfinite(start) or not math.isfinite(end)
                    or start < 0 or end < start):
                raise ValueError(f"segment {index} has invalid timestamps")
            if not isinstance(text, str) or not text.strip() or len(text) > 20_000:
                raise ValueError(f"segment {index} text must contain 1 to 20000 characters")
            if speaker_id is not None and (
                not isinstance(speaker_id, str)
                or not re.fullmatch(r"speaker_[1-9][0-9]*", speaker_id)
            ):
                raise ValueError(f"segment {index} has an invalid speaker ID")
            normalized.append({
                "start_seconds": float(start), "end_seconds": float(end),
                "text": text.strip(), "speaker_id": speaker_id,
            })
        return normalized

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

    def assign_segment_speaker(
        self, session_id: str, segment_index: int, speaker_id: str | None,
    ) -> StoredSession:
        """Correct or clear one Meeting segment's speaker assignment."""
        if not isinstance(segment_index, int) or isinstance(segment_index, bool) or segment_index < 0:
            raise ValueError("segment_index must be a non-negative integer")
        if speaker_id is not None and (
            not isinstance(speaker_id, str)
            or not re.fullmatch(r"speaker_[1-9][0-9]*", speaker_id)
        ):
            raise ValueError("speaker_id must look like speaker_1 or be omitted to clear the label")
        session = self.get(session_id)
        if session.mode != "meeting":
            raise ValueError("speaker assignments are only supported for Meeting sessions")
        if segment_index >= len(session.segments):
            raise ValueError("segment_index is outside this transcript")
        segments = [dict(segment) for segment in session.segments]
        segments[segment_index]["speaker_id"] = speaker_id
        overrides = session.outputs.get("speaker_overrides", {})
        if not isinstance(overrides, dict):
            overrides = {}
        overrides = {**overrides, str(segment_index): speaker_id}
        return self.update(session_id, segments=segments,
                           outputs={**session.outputs, "speaker_overrides": overrides})

    def merge_speakers(self, session_id: str, source_id: str, target_id: str) -> StoredSession:
        """Merge one Meeting speaker identity into another across all segments."""
        for speaker_id in (source_id, target_id):
            if not isinstance(speaker_id, str) or not re.fullmatch(r"speaker_[1-9][0-9]*", speaker_id):
                raise ValueError("speaker IDs must look like speaker_1")
        if source_id == target_id:
            raise ValueError("source and target speaker IDs must be different")
        session = self.get(session_id)
        if session.mode != "meeting":
            raise ValueError("speaker identities can only be merged for Meeting sessions")
        if not any(segment["speaker_id"] == source_id for segment in session.segments):
            raise ValueError("source speaker is not assigned to any transcript segment")
        segments = [
            {**segment, "speaker_id": target_id if segment["speaker_id"] == source_id else segment["speaker_id"]}
            for segment in session.segments
        ]
        speaker_names = session.outputs.get("speaker_names", {})
        if not isinstance(speaker_names, dict):
            speaker_names = {}
        merged_names = dict(speaker_names)
        if target_id not in merged_names and source_id in merged_names:
            merged_names[target_id] = merged_names[source_id]
        merged_names.pop(source_id, None)
        overrides = session.outputs.get("speaker_overrides", {})
        if not isinstance(overrides, dict):
            overrides = {}
        overrides = dict(overrides)
        for index, segment in enumerate(session.segments):
            if segment["speaker_id"] == source_id:
                overrides[str(index)] = target_id
        for index, speaker_id in tuple(overrides.items()):
            if speaker_id == source_id:
                overrides[index] = target_id
        outputs = {
            **session.outputs,
            "speaker_names": merged_names,
            "speaker_overrides": overrides,
        }
        return self.update(session_id, segments=segments, outputs=outputs)

    def apply_diarization(
        self, session_id: str, turns: list[dict[str, Any]],
    ) -> StoredSession:
        """Align automatic speaker turns to timestamped Meeting segments.

        A segment is labeled only when one speaker covers a strict majority of
        its duration. Existing labels are preserved so a repeated analysis
        cannot overwrite a user's corrections.
        """
        if not isinstance(turns, list) or len(turns) > MAX_TRANSCRIPT_SEGMENTS:
            raise ValueError(f"turns must be a list of at most {MAX_TRANSCRIPT_SEGMENTS} entries")
        normalized_turns: list[tuple[float, float, str]] = []
        for index, turn in enumerate(turns):
            if not isinstance(turn, dict):
                raise ValueError(f"turn {index} must be a mapping")
            start = turn.get("start_seconds")
            end = turn.get("end_seconds")
            speaker_id = turn.get("speaker_id")
            if (isinstance(start, bool) or not isinstance(start, (int, float))
                    or isinstance(end, bool) or not isinstance(end, (int, float))
                    or not math.isfinite(start) or not math.isfinite(end)
                    or start < 0 or end <= start
                    or not isinstance(speaker_id, str)
                    or not re.fullmatch(r"speaker_[1-9][0-9]*", speaker_id)):
                raise ValueError(f"turn {index} has invalid timestamps or speaker ID")
            normalized_turns.append((float(start), float(end), speaker_id))
        normalized_turns.sort(key=lambda turn: (turn[0], turn[1], turn[2]))

        session = self.get(session_id)
        if session.mode != "meeting":
            raise ValueError("automatic speaker assignments are only supported for Meeting sessions")
        segments = [dict(segment) for segment in session.segments]
        ordered_segments = sorted(
            ((index, segment) for index, segment in enumerate(segments)),
            key=lambda item: (item[1]["start_seconds"], item[1]["end_seconds"], item[0]),
        )
        active: list[tuple[float, float, str]] = []
        turn_index = 0
        assigned_speakers: set[str] = set()
        overrides = session.outputs.get("speaker_overrides", {})
        if not isinstance(overrides, dict):
            overrides = {}
        for segment_index, segment in ordered_segments:
            segment_start = segment["start_seconds"]
            segment_end = segment["end_seconds"]
            if (str(segment_index) in overrides or segment.get("speaker_id") is not None
                    or segment_end <= segment_start):
                continue
            while turn_index < len(normalized_turns) and normalized_turns[turn_index][0] < segment_end:
                active.append(normalized_turns[turn_index])
                turn_index += 1
            active = [turn for turn in active if turn[1] > segment_start]
            overlap_by_speaker: dict[str, float] = {}
            for turn_start, turn_end, speaker_id in active:
                overlap = min(segment_end, turn_end) - max(segment_start, turn_start)
                if overlap > 0:
                    overlap_by_speaker[speaker_id] = overlap_by_speaker.get(speaker_id, 0.0) + overlap
            if not overlap_by_speaker:
                continue
            ranked = sorted(overlap_by_speaker.items(), key=lambda item: (-item[1], item[0]))
            best_speaker, best_overlap = ranked[0]
            tied = len(ranked) > 1 and math.isclose(best_overlap, ranked[1][1], rel_tol=1e-9, abs_tol=1e-9)
            if not tied and best_overlap > (segment_end - segment_start) / 2:
                segments[segment_index]["speaker_id"] = best_speaker
                assigned_speakers.add(best_speaker)

        diarization = {"state": "complete", "speaker_count": len(assigned_speakers)}
        return self.update(session_id, segments=segments, outputs={**session.outputs, "diarization": diarization})

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

