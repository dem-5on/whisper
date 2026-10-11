from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from transcriber.session_export import export_session_markdown, render_session_markdown
from transcriber.session_store import SessionStore


class SessionExportTests(unittest.TestCase):
    def test_markdown_contains_transcript_translation_and_structured_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "data")
            session = store.create("lecture")
            session = store.update(
                session.id,
                state="ready",
                source_transcript="Kia ora, welcome.",
                english_translation="Hello, welcome.",
                outputs={"notes": ["Topic one", "Topic two"], "questions": ["Why?"]},
            )

            markdown = render_session_markdown(session)
            exported = export_session_markdown(session, Path(directory) / "lecture.md")

            self.assertIn("# Lecture", markdown)
            self.assertIn("Kia ora, welcome.", markdown)
            self.assertIn("## English translation", markdown)
            self.assertIn("- Topic one", markdown)
            self.assertIn("- Why?", markdown)
            self.assertEqual(exported.read_text(encoding="utf-8"), markdown)

    def test_export_never_overwrites_an_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "data")
            session = store.create("meeting")
            target = Path(directory) / "meeting.md"
            target.write_text("keep this", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                export_session_markdown(session, target)

            self.assertEqual(target.read_text(encoding="utf-8"), "keep this")


if __name__ == "__main__":
    unittest.main()
