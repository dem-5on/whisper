from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from transcriber.session_store import SessionStore, SessionStoreError


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root)

    def test_create_keeps_each_session_audio_in_a_dedicated_local_folder(self) -> None:
        lecture = self.store.create("lecture")
        meeting = self.store.create("meeting")

        self.assertNotEqual(lecture.id, meeting.id)
        self.assertEqual(lecture.state, "recording")
        self.assertEqual(lecture.audio_path, self.root / "sessions" / lecture.id / "recording.wav")
        self.assertTrue(lecture.audio_path.parent.is_dir())
        self.assertEqual({item.mode for item in self.store.list()}, {"meeting", "lecture"})
        self.assertEqual([item.id for item in self.store.list(mode="lecture")], [lecture.id])

    def test_session_pages_are_ordered_and_report_remaining_results(self) -> None:
        created = [self.store.create("lecture") for _ in range(3)]
        expected = [item.id for item in self.store.list(limit=10)]

        first, first_has_more = self.store.list_page(limit=2, offset=0)
        second, second_has_more = self.store.list_page(limit=2, offset=2)

        self.assertEqual([item.id for item in first], expected[:2])
        self.assertEqual([item.id for item in second], expected[2:])
        self.assertTrue(first_has_more)
        self.assertFalse(second_has_more)
        self.assertEqual(set(expected), {item.id for item in created})

    def test_session_page_rejects_invalid_offset(self) -> None:
        for offset in (-1, True, "1"):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.store.list_page(offset=offset)  # type: ignore[arg-type]

    def test_session_pages_filter_by_mode_and_search_literal_transcript_text(self) -> None:
        lecture = self.store.create("lecture")
        self.store.update(lecture.id, source_transcript="Use Kubernetes at 100% capacity")
        meeting = self.store.create("meeting")
        self.store.update(meeting.id, english_translation="Discuss Kubernetes next week")
        self.store.create("dictation")

        by_mode, _ = self.store.list_page(mode="lecture")
        by_source, _ = self.store.list_page(search="100%")
        by_translation, _ = self.store.list_page(search="next week")

        self.assertEqual([item.id for item in by_mode], [lecture.id])
        self.assertEqual([item.id for item in by_source], [lecture.id])
        self.assertEqual([item.id for item in by_translation], [meeting.id])

    def test_session_search_rejects_oversized_query(self) -> None:
        with self.assertRaises(ValueError):
            self.store.list_page(search="x" * 201)

    def test_connection_context_closes_handle_after_transaction(self) -> None:
        connection = sqlite3.connect(":memory:")
        with patch.object(self.store, "_connect", return_value=connection):
            with self.store._connection() as active_connection:
                active_connection.execute("CREATE TABLE check_close (value INTEGER)")

        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_transcripts_and_structured_outputs_survive_store_reopen(self) -> None:
        session = self.store.create("lecture")
        session.audio_path.write_bytes(b"wave bytes")
        updated = self.store.update(
            session.id,
            state="ready",
            source_transcript="Maakye, class.",
            english_translation="Good morning, class.",
            outputs={"notes": ["Topic one"], "questions": ["What is topic one?"]},
            finished=True,
        )

        reopened = SessionStore(self.root).get(session.id)
        self.assertEqual(reopened.state, "ready")
        self.assertEqual(reopened.source_transcript, "Maakye, class.")
        self.assertEqual(reopened.english_translation, "Good morning, class.")
        self.assertEqual(reopened.outputs, {"notes": ["Topic one"], "questions": ["What is topic one?"]})
        self.assertIsNotNone(reopened.finished_at)
        self.assertTrue(reopened.audio_path.is_file())
        self.assertEqual(updated.id, reopened.id)

    def test_meeting_speaker_names_persist_and_validate(self) -> None:
        meeting = self.store.create("meeting")

        renamed = self.store.rename_speaker(meeting.id, "speaker_1", "Dr. Mensah")

        self.assertEqual(renamed.outputs["speaker_names"], {"speaker_1": "Dr. Mensah"})
        self.assertEqual(SessionStore(self.root).get(meeting.id).outputs, renamed.outputs)
        lecture = self.store.create("lecture")
        with self.assertRaisesRegex(ValueError, "only supported for Meeting"):
            self.store.rename_speaker(lecture.id, "speaker_1", "Teacher")
        with self.assertRaises(ValueError):
            self.store.rename_speaker(meeting.id, "speaker_0", "Invalid ID")
        with self.assertRaises(ValueError):
            self.store.rename_speaker(meeting.id, "speaker_1", "")

    def test_update_rejects_invalid_state_and_non_json_outputs(self) -> None:
        session = self.store.create("meeting")
        with self.assertRaises(ValueError):
            self.store.update(session.id, state="paused")
        with self.assertRaisesRegex(ValueError, "JSON-compatible"):
            self.store.update(session.id, outputs={"bad": object()})

    def test_delete_removes_record_and_audio(self) -> None:
        session = self.store.create("dictation")
        session.audio_path.write_bytes(b"recording")

        self.store.delete(session.id)

        self.assertFalse(session.audio_path.parent.exists())
        with self.assertRaises(KeyError):
            self.store.get(session.id)

    def test_failed_audio_removal_keeps_session_indexed_for_retry(self) -> None:
        session = self.store.create("meeting")
        session.audio_path.write_bytes(b"recording")

        with patch("transcriber.session_store.shutil.rmtree", side_effect=PermissionError("locked")):
            with self.assertRaises(SessionStoreError):
                self.store.delete(session.id)

        self.assertTrue(session.audio_path.is_file())
        self.assertEqual(self.store.get(session.id).id, session.id)

    def test_recovery_marks_unfinished_long_sessions_and_preserves_audio(self) -> None:
        lecture = self.store.create("lecture")
        lecture.audio_path.write_bytes(b"partial wave")
        meeting = self.store.create("meeting")
        self.store.update(meeting.id, state="transcribing", finished=True)
        ready = self.store.create("lecture")
        self.store.update(ready.id, state="ready", finished=True)
        dictation = self.store.create("dictation")

        self.assertEqual(self.store.recover_interrupted(), 2)

        recovered_lecture = self.store.get(lecture.id)
        recovered_meeting = self.store.get(meeting.id)
        self.assertEqual(recovered_lecture.state, "error")
        self.assertIn("may be incomplete", recovered_lecture.error)
        self.assertTrue(recovered_lecture.audio_path.is_file())
        self.assertEqual(recovered_lecture.audio_path.read_bytes(), b"partial wave")
        self.assertEqual(recovered_meeting.state, "error")
        self.assertEqual(self.store.get(ready.id).state, "ready")
        self.assertEqual(self.store.get(dictation.id).state, "recording")
        self.assertEqual(self.store.recover_interrupted(), 0)

    def test_unknown_mode_and_missing_session_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create("podcast")
        with self.assertRaises(KeyError):
            self.store.get("missing")

    def test_unserializable_outputs_do_not_change_existing_record(self) -> None:
        session = self.store.create("lecture")
        with self.assertRaisesRegex(ValueError, "JSON-compatible"):
            self.store.update(session.id, state="ready", outputs={"broken": object()})
        current = self.store.get(session.id)
        self.assertEqual(current.state, "recording")
        self.assertEqual(current.outputs, {})


if __name__ == "__main__":
    unittest.main()
