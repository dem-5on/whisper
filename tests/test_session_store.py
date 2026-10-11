from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from transcriber.session_store import SessionStore


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
