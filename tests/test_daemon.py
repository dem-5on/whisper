from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from transcriber.audio import AudioSource, parse_wpctl_status
from transcriber.config import Config, DeliveryConfig, UiConfig, _parse
from transcriber.daemon import (
    TranscriberDaemon,
    _configure_logging,
    _existing_daemon_exit_code,
    _is_daemon_status_response,
)
from transcriber.state import State


class DaemonLoggingTests(unittest.TestCase):
    def test_packaged_windows_sidecar_uses_a_bounded_user_log_file(self) -> None:
        from logging.handlers import RotatingFileHandler

        with tempfile.TemporaryDirectory() as temporary:
            log_root = Path(temporary)
            with patch("transcriber.daemon.default_data_dir", return_value=log_root), patch(
                "transcriber.daemon.logging.basicConfig"
            ) as basic_config:
                _configure_logging(verbose=False, log_to_file=True)

            handlers = basic_config.call_args.kwargs["handlers"]
            file_handlers = [handler for handler in handlers if isinstance(handler, RotatingFileHandler)]
            try:
                self.assertEqual(len(file_handlers), 1)
                self.assertEqual(Path(file_handlers[0].baseFilename), log_root / "logs" / "daemon.log")
                self.assertEqual(file_handlers[0].maxBytes, 2 * 1024 * 1024)
                self.assertEqual(file_handlers[0].backupCount, 3)
                self.assertTrue((log_root / "logs" / "daemon.log").exists())
            finally:
                for handler in handlers:
                    handler.close()

    def test_regular_daemon_mode_does_not_create_a_file_log(self) -> None:
        with patch("transcriber.daemon.logging.basicConfig") as basic_config:
            _configure_logging(verbose=False)

        handlers = basic_config.call_args.kwargs["handlers"]
        self.assertEqual(len(handlers), 1)
        self.assertIsInstance(handlers[0], logging.StreamHandler)


class FakeRecorder:
    def __init__(self, audio: Path) -> None:
        self.audio = audio
        self.started = False
        self.cancelled = False
        self.config = None

    def start(self, destination: Path | None = None) -> Path:
        self.started = True
        if destination is not None:
            self.audio = destination
        return self.audio

    def stop(self) -> Path:
        self.started = False
        self.audio.write_bytes(b"wav")
        return self.audio

    def cancel(self) -> None:
        self.started = False
        self.cancelled = True
        self.audio.unlink(missing_ok=True)


class FakeBackend:
    def __init__(self, text: str = "hello") -> None:
        self.text = text

    def transcribe(self, _audio: Path) -> str:
        return self.text


class FakeDelivery:
    def __init__(self) -> None:
        self.inserted: list[str] = []
        self.submitted = False
        self.done = threading.Event()

    def insert(self, text: str) -> None:
        self.inserted.append(text)

    def submit(self) -> None:
        self.submitted = True
        self.done.set()


class DaemonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.audio = Path(self.tmp.name) / "audio.wav"
        self.data = Path(self.tmp.name) / "data"
        self.daemon = TranscriberDaemon(Config(), data_dir=self.data)
        self.recorder = FakeRecorder(self.audio)
        self.daemon.recorder = self.recorder  # type: ignore[assignment]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def wait_for(self, *states: State) -> None:
        for _ in range(200):
            if self.daemon.state in states:
                return
            time.sleep(0.01)
        self.fail(f"daemon did not reach {states}; at {self.daemon.state}")

    def test_sessions_command_exposes_page_metadata(self) -> None:
        first = self.daemon._session_store.create("lecture")
        self.daemon._session_store.update(first.id, source_transcript="Kubernetes lecture")
        self.daemon._session_store.create("meeting")

        page = self.daemon.command("sessions", {"limit": 1, "offset": 0})

        self.assertEqual(page["ok"], "true")
        self.assertEqual(page["offset"], 0)
        self.assertEqual(page["limit"], 1)
        self.assertTrue(page["has_more"])
        self.assertEqual(len(page["sessions"]), 1)
        last_page = self.daemon.command("sessions", {"limit": 1, "offset": 1})
        self.assertFalse(last_page["has_more"])
        self.assertNotEqual(page["sessions"][0]["id"], first.id)
        filtered = self.daemon.command("sessions", {"mode": "lecture", "search": "kubernetes"})
        self.assertEqual([item["id"] for item in filtered["sessions"]], [first.id])
        self.assertEqual(filtered["mode"], "lecture")

    def test_rename_speaker_command_persists_meeting_display_name(self) -> None:
        meeting = self.daemon._session_store.create("meeting")

        renamed = self.daemon.command("rename_speaker", {
            "session_id": meeting.id, "speaker_id": "speaker_1", "name": "Dr. Mensah",
        })

        self.assertEqual(renamed["ok"], "true")
        self.assertEqual(renamed["name"], "Dr. Mensah")
        listed = self.daemon.command("sessions", {"mode": "meeting"})["sessions"]
        self.assertEqual(listed[0]["speaker_names"], {"speaker_1": "Dr. Mensah"})

    def test_meeting_speaker_correction_commands_update_segments(self) -> None:
        meeting = self.daemon._session_store.create("meeting")
        self.daemon._session_store.update(meeting.id, segments=[
            {"start_seconds": 0, "end_seconds": 1, "text": "Hello", "speaker_id": "speaker_1"},
            {"start_seconds": 1, "end_seconds": 2, "text": "Hi", "speaker_id": "speaker_2"},
            {"start_seconds": 2, "end_seconds": 3, "text": "Again", "speaker_id": None},
        ])

        corrected = self.daemon.command("assign_segment_speaker", {
            "session_id": meeting.id, "segment_index": 2, "speaker_id": "speaker_2",
        })
        self.daemon.command("rename_speaker", {
            "session_id": meeting.id, "speaker_id": "speaker_2", "name": "Ama",
        })
        merged = self.daemon.command("merge_speakers", {
            "session_id": meeting.id, "source_id": "speaker_1", "target_id": "speaker_2",
        })

        self.assertEqual(corrected["ok"], "true")
        self.assertEqual(merged["ok"], "true")
        saved = self.daemon._session_store.get(meeting.id)
        self.assertEqual([segment["speaker_id"] for segment in saved.segments], [
            "speaker_2", "speaker_2", "speaker_2",
        ])
        self.assertEqual(saved.outputs["speaker_names"], {"speaker_2": "Ama"})

    def test_speaker_correction_rejects_non_meeting_session(self) -> None:
        lecture = self.daemon._session_store.create("lecture")

        response = self.daemon.command("assign_segment_speaker", {
            "session_id": lecture.id, "segment_index": 0, "speaker_id": "speaker_1",
        })

        self.assertEqual(response["ok"], "false")
        self.assertIn("Meeting", response["error"])

    def test_get_session_returns_timestamped_segments(self) -> None:
        meeting = self.daemon._session_store.create("meeting")
        self.daemon._session_store.update(meeting.id, segments=[{
            "start_seconds": 3.5, "end_seconds": 4.25,
            "text": "Let's begin.", "speaker_id": "speaker_1",
        }])

        response = self.daemon.command("get_session", {"session_id": meeting.id})

        self.assertEqual(response["ok"], "true")
        self.assertEqual(response["session"]["segments"][0]["speaker_id"], "speaker_1")
        self.assertEqual(response["session"]["segments"][0]["start_seconds"], 3.5)

    def test_existing_daemon_adoption_is_limited_to_authenticated_windows_sidecars(self) -> None:
        self.assertEqual(_existing_daemon_exit_code("nt", "per-user-token"), 75)
        self.assertEqual(_existing_daemon_exit_code("nt", None), 1)
        self.assertEqual(_existing_daemon_exit_code("posix", "per-user-token"), 1)

    def test_existing_ipc_endpoint_must_answer_with_a_daemon_state_before_adoption(self) -> None:
        self.assertTrue(_is_daemon_status_response({"ok": "true", "state": "IDLE"}))
        self.assertFalse(_is_daemon_status_response({"ok": "false", "state": "IDLE"}))
        self.assertFalse(_is_daemon_status_response({"ok": "true", "state": "not-a-state"}))

    def test_toggle_processes_delivers_and_returns_idle(self) -> None:
        delivery = FakeDelivery()
        with patch("transcriber.daemon.make_backend", return_value=FakeBackend(" kubectl get pods ")), patch("transcriber.daemon.make_delivery", return_value=delivery):
            self.assertEqual(self.daemon.command("toggle")["state"], "RECORDING")
            self.assertEqual(self.daemon.command("toggle")["state"], "PROCESSING")
            self.wait_for(State.IDLE)
        self.assertEqual(delivery.inserted, [" kubectl get pods "])
        self.assertFalse(delivery.submitted)
        # Original temp is cleaned up, preserved copy is kept for retry.
        self.assertFalse(self.audio.exists())
        self.assertTrue((self.data / "last.wav").exists())
        self.assertEqual((self.data / "last.txt").read_text(), " kubectl get pods ")
        history = self.daemon.command("history")
        self.assertEqual(history["ok"], "true")
        self.assertEqual(history["history"][0]["mode"], "dictation")
        self.assertEqual(history["history"][0]["transcript"], " kubectl get pods ")
        dictation_audio = self.data / "sessions" / history["history"][0]["id"] / "recording.wav"
        self.assertFalse(dictation_audio.exists())
        restarted = TranscriberDaemon(Config(), data_dir=self.data)
        self.assertEqual(restarted.command("history")["history"][0]["transcript"], " kubectl get pods ")

    def test_hosted_final_is_delivered_without_a_second_batch_transcription(self) -> None:
        delivery = FakeDelivery()
        self.daemon.command("toggle")
        self.daemon._hosted_live_session = True
        self.daemon._hosted_final_text = "Remote final transcript"
        self.daemon._hosted_final_event.set()
        with patch("transcriber.daemon.make_backend", side_effect=AssertionError("must use hosted final")), patch(
            "transcriber.daemon.make_delivery", return_value=delivery
        ):
            self.daemon.command("toggle")
            self.wait_for(State.IDLE)
        self.assertEqual(delivery.inserted, ["Remote final transcript"])
        self.assertEqual(self.daemon.command("status")["last_backend"], "hosted-whisper")

    def test_hosted_usage_limit_falls_back_to_local_and_reports_why(self) -> None:
        delivery = FakeDelivery()
        self.daemon.command("toggle")
        self.daemon._hosted_live_session = True
        self.daemon._hosted_live_error = "usage_limit: cloud allowance is used"
        self.daemon._hosted_final_event.set()
        with patch("transcriber.daemon.make_backend", return_value=FakeBackend("Local transcript")) as make_backend, patch(
            "transcriber.daemon.make_delivery", return_value=delivery
        ):
            self.daemon.command("toggle")
            self.wait_for(State.IDLE)

        self.assertEqual(delivery.inserted, ["Local transcript"])
        self.assertEqual(make_backend.call_args.args[0].backend, "local")
        status = self.daemon.command("status")
        self.assertEqual(status["last_backend"], "local-fallback")
        self.assertIn("allowance reached", status["last_notice"])

    def test_cancel_discards_recording_without_transcribing(self) -> None:
        with patch("transcriber.daemon.make_backend") as backend:
            self.daemon.command("toggle")
            response = self.daemon.command("cancel")
        self.assertEqual(response["state"], "IDLE")
        self.assertTrue(self.recorder.cancelled)
        backend.assert_not_called()
        self.assertEqual(self.daemon.command("history")["history"], [])

    def test_cancel_retains_long_session_audio(self) -> None:
        started = self.daemon.command("start_session", {
            "mode": "lecture", "consent_acknowledged": True,
        })
        self.assertEqual(started["state"], "RECORDING")
        session_id = self.daemon._stored_session_id
        self.assertIsNotNone(session_id)
        response = self.daemon.command("cancel")
        self.assertEqual(response["state"], "IDLE")
        assert session_id is not None
        saved = self.daemon._session_store.get(session_id)
        self.assertEqual(saved.state, "error")
        self.assertIn("retained", saved.error)
        self.assertEqual(len(self.daemon.command("sessions")["sessions"]), 1)

    def test_restart_repairs_truncated_long_recording(self) -> None:
        import wave

        stored = self.daemon._session_store.create("lecture")
        frames = b"\x01\x02" * 8000
        with wave.open(str(stored.audio_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(frames)
        with open(stored.audio_path, "r+b") as stream:
            stream.seek(0, 2)
            stream.write(frames)
        recovered = self.daemon._recover_interrupted_sessions()
        self.assertEqual(recovered, [stored.id])
        saved = self.daemon._session_store.get(stored.id)
        self.assertEqual(saved.state, "error")
        with wave.open(str(stored.audio_path), "rb") as wav:
            self.assertEqual(wav.getnframes(), 16000)

    def test_low_disk_refuses_new_recording(self) -> None:
        with patch.object(self.daemon, "_disk_free_bytes", return_value=1):
            refused = self.daemon.command("toggle")
        self.assertEqual(refused["ok"], "false")
        self.assertIn("disk space", refused["error"])
        self.assertFalse(self.recorder.started)

    def test_history_rejects_invalid_limit(self) -> None:
        response = self.daemon.command("history", {"limit": 0})
        self.assertEqual(response["ok"], "false")
        self.assertIn("between 1 and 1000", response["error"])

    def test_delete_session_removes_saved_dictation_history(self) -> None:
        with patch("transcriber.daemon.make_backend", return_value=FakeBackend("saved text")), patch(
            "transcriber.daemon.make_delivery", return_value=FakeDelivery()
        ):
            self.daemon.command("toggle")
            self.daemon.command("toggle")
            self.wait_for(State.IDLE)
        session_id = self.daemon.command("history")["history"][0]["id"]
        deleted = self.daemon.command("delete_session", {"session_id": session_id})
        self.assertEqual(deleted["ok"], "true")
        self.assertEqual(self.daemon.command("history")["history"], [])
        self.assertEqual(self.daemon.command("delete_session", {"session_id": session_id})["ok"], "false")

    def test_export_session_writes_markdown_without_overwriting(self) -> None:
        stored = self.daemon._session_store.create("lecture")
        stored.audio_path.write_bytes(b"RIFF" + b"\x00" * 40 + b"audio")
        self.daemon._session_store.update(
            stored.id,
            state="ready",
            source_transcript="The lesson begins.",
            outputs={"notes": ["First topic"]},
            finished=True,
        )
        destination = self.data / "exports" / "lecture.md"
        destination.parent.mkdir()

        exported = self.daemon.command("export_session", {
            "session_id": stored.id, "output": str(destination),
        })

        self.assertEqual(exported["ok"], "true")
        self.assertEqual(Path(exported["export_path"]), destination)
        self.assertIn("The lesson begins.", destination.read_text(encoding="utf-8"))
        audio_destination = self.data / "exports" / "lecture.wav"
        audio_export = self.daemon.command("export_session_audio", {
            "session_id": stored.id, "output": str(audio_destination),
        })
        self.assertEqual(audio_export["ok"], "true")
        self.assertEqual(audio_destination.read_bytes(), stored.audio_path.read_bytes())
        refused = self.daemon.command("export_session", {
            "session_id": stored.id, "output": str(destination),
        })
        self.assertEqual(refused["ok"], "false")
        self.assertIn("already exists", refused["error"])

    def test_export_session_docx_uses_default_documents_path(self) -> None:
        from unittest.mock import patch

        stored = self.daemon._session_store.create("dictation")
        self.daemon._session_store.update(
            stored.id, state="ready", source_transcript="Docx please.", finished=True,
        )
        with patch("transcriber.session_export.Path.home", return_value=self.data):
            exported = self.daemon.command("export_session", {
                "session_id": stored.id, "format": "docx",
            })
        self.assertEqual(exported["ok"], "true")
        self.assertTrue(exported["export_path"].endswith(".docx"))
        self.assertTrue(exported["export_path"].startswith(str(self.data)))
        bad = self.daemon.command("export_session", {
            "session_id": stored.id, "format": "pdf",
        })
        self.assertEqual(bad["ok"], "false")

    def test_dictionary_commands_persist_terms_and_supply_local_recognition_hint(self) -> None:
        added = self.daemon.command("dictionary", {"action": "add", "term": "CTranslate2"})
        duplicate = self.daemon.command("dictionary", {"action": "add", "term": "ctranslate2"})
        listed = self.daemon.command("dictionary", {"action": "list"})
        self.assertEqual(added["ok"], "true")
        self.assertTrue(added["changed"])
        self.assertFalse(duplicate["changed"])
        self.assertEqual(listed["terms"], ["CTranslate2"])

        backend = type("PromptBackend", (), {"set_initial_prompt": lambda self, value: setattr(self, "prompt", value)})()
        with patch("transcriber.daemon.make_backend", return_value=backend):
            self.daemon._make_backend(self.daemon.config.transcription)
        self.assertIn("CTranslate2", backend.prompt)

        removed = self.daemon.command("dictionary", {"action": "remove", "term": "ctranslate2"})
        self.assertTrue(removed["changed"])
        self.assertEqual(self.daemon.command("dictionary")["terms"], [])

    def test_active_session_cannot_be_deleted(self) -> None:
        self.daemon.command("toggle")
        session_id = self.daemon._stored_session_id
        response = self.daemon.command("delete_session", {"session_id": session_id})
        self.assertEqual(response["ok"], "false")
        self.assertEqual(response["error"], "Cannot delete the active session")
        self.daemon.command("cancel")

    def test_long_session_requires_consent_and_preserves_audio_without_typing_it(self) -> None:
        refused = self.daemon.command("start_session", {"mode": "meeting"})
        self.assertEqual(refused["ok"], "false")
        self.assertIn("informed", refused["error"])
        self.assertFalse(self.recorder.started)

        remote_config = dataclasses.replace(
            self.daemon.config.transcription, backend="openrouter", model="openai/whisper-large-v3"
        )
        self.daemon.config = dataclasses.replace(self.daemon.config, transcription=remote_config)
        delivery = FakeDelivery()
        with patch("transcriber.daemon.make_backend", return_value=FakeBackend("lecture words")) as backend, patch(
            "transcriber.daemon.make_delivery", return_value=delivery
        ) as make_delivery:
            started = self.daemon.command("start_session", {
                "mode": "meeting", "consent_acknowledged": True,
            })
            self.assertEqual(started["state"], "RECORDING")
            self.assertIsNone(self.daemon._timer)
            session_id = self.daemon._stored_session_id
            audio_path = self.recorder.audio
            self.assertEqual(audio_path, self.data / "sessions" / session_id / "recording.wav")
            finished = self.daemon.command("stop_session")
            self.assertEqual(finished["state"], "PROCESSING")
            self.wait_for(State.IDLE)

        backend.assert_called_once()
        self.assertEqual(backend.call_args.args[0].backend, "local")
        make_delivery.assert_not_called()
        self.assertEqual(delivery.inserted, [])
        saved = self.daemon._session_store.get(session_id)
        self.assertEqual(saved.mode, "meeting")
        self.assertEqual(saved.source_transcript, "lecture words")
        self.assertEqual(saved.segments, [])
        session_list = self.daemon.command("sessions")["sessions"]
        self.assertEqual(session_list[0]["mode"], "meeting")
        self.assertTrue(session_list[0]["audio_available"])
        self.assertEqual(audio_path.read_bytes(), b"wav")

    def _make_lecture_session(self, transcript="Photosynthesis converts light."):
        stored = self.daemon._session_store.create("lecture")
        return self.daemon._session_store.update(
            stored.id, state="ready", source_transcript=transcript, finished=True,
        )

    def test_lecture_analysis_stores_all_stages(self) -> None:
        stored = self._make_lecture_session()
        calls: list[str] = []

        def fake_post(kind: str, text: str, language: str | None = None):
            calls.append(kind)
            if kind == "translation":
                return "ready", {"language": "en", "translation": None}
            if kind == "topics":
                return "ready", [{"title": "Intro", "summary": "Start", "start_segment": 0, "end_segment": 0}]
            if kind == "terms":
                return "ready", [{"term": "Photosynthesis", "definition": "Plants make food."}]
            if kind == "questions":
                return "ready", ["Why do plants need light?"]
            return "ready", f"# Notes\n- {text[:20]}"

        with patch.object(self.daemon, "_analysis_post", side_effect=fake_post):
            self.daemon._run_lecture_analysis(stored.id)
        saved = self.daemon._session_store.get(stored.id)
        self.assertEqual(saved.source_transcript, "Photosynthesis converts light.")
        self.assertTrue(saved.outputs["lecture_notes"].startswith("# Notes"))
        self.assertEqual(saved.outputs["topic_groups"][0]["title"], "Intro")
        self.assertEqual(saved.outputs["key_terms"][0]["term"], "Photosynthesis")
        self.assertEqual(saved.outputs["review_questions"], ["Why do plants need light?"])
        self.assertEqual(saved.english_translation, "")
        states = saved.outputs["analysis_state"]
        self.assertTrue(all(states[kind]["state"] == "ready" for kind in (
            "translation", "notes", "topics", "terms", "questions")))
        self.assertEqual(calls[0], "translation")

    def test_lecture_analysis_blocked_keeps_transcript(self) -> None:
        stored = self._make_lecture_session()
        with patch.object(
            self.daemon, "_analysis_post", return_value=("blocked", "Subscription allowance exhausted.")
        ):
            self.daemon._run_lecture_analysis(stored.id)
        saved = self.daemon._session_store.get(stored.id)
        self.assertEqual(saved.source_transcript, "Photosynthesis converts light.")
        self.assertNotIn("lecture_notes", saved.outputs)
        self.assertEqual(saved.outputs["analysis_state"]["notes"]["state"], "blocked")

    def test_lecture_analysis_queued_then_retried(self) -> None:
        stored = self._make_lecture_session()
        with patch.object(
            self.daemon, "_analysis_post", return_value=("queued", "Service unreachable.")
        ):
            self.daemon._run_lecture_analysis(stored.id)
        saved = self.daemon._session_store.get(stored.id)
        self.assertEqual(saved.outputs["analysis_state"]["terms"]["state"], "queued")

        def fake_post(kind: str, text: str, language: str | None = None):
            if kind == "translation":
                return "ready", {"language": "en", "translation": None}
            if kind == "topics":
                return "ready", []
            if kind == "terms":
                return "ready", [{"term": "Light", "definition": "Energy."}]
            if kind == "questions":
                return "ready", ["What?"]
            return "ready", "notes text"

        with patch.object(self.daemon, "_analysis_post", side_effect=fake_post):
            retried = self.daemon.command("retry_analysis", {"session_id": stored.id})
            self.assertEqual(retried["ok"], "true")
            import time

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                saved = self.daemon._session_store.get(stored.id)
                if "key_terms" in saved.outputs:
                    break
                time.sleep(0.05)
        saved = self.daemon._session_store.get(stored.id)
        self.assertEqual(saved.outputs["key_terms"], [{"term": "Light", "definition": "Energy."}])

    def test_sweep_retries_queued_analysis_on_reconnect(self) -> None:
        stored = self._make_lecture_session()
        with patch.object(
            self.daemon, "_analysis_post", return_value=("queued", "Service unreachable.")
        ):
            self.daemon._run_lecture_analysis(stored.id)
        saved = self.daemon._session_store.get(stored.id)
        self.assertEqual(saved.outputs["analysis_state"]["notes"]["state"], "queued")

        def fake_post(kind: str, text: str, language: str | None = None):
            if kind == "translation":
                return "ready", {"language": "en", "translation": None}
            if kind == "topics":
                return "ready", []
            if kind == "terms":
                return "ready", [{"term": "Light", "definition": "Energy."}]
            if kind == "questions":
                return "ready", ["What?"]
            return "ready", "notes text"

        with patch.object(self.daemon, "_analysis_post", side_effect=fake_post):
            self.daemon._sweep_timer.cancel()
            self.daemon._sweep_queued_analysis()
        saved = self.daemon._session_store.get(stored.id)
        states = saved.outputs["analysis_state"]
        self.assertTrue(all(states[kind]["state"] == "ready" for kind in (
            "translation", "notes", "topics", "terms", "questions")))
        self.assertIn("completed", self.daemon._last_notice)

    def test_sweep_skips_blocked_and_busy(self) -> None:
        stored = self._make_lecture_session()
        with patch.object(
            self.daemon, "_analysis_post", return_value=("blocked", "No subscription.")
        ):
            self.daemon._run_lecture_analysis(stored.id)
        with patch.object(
            self.daemon, "_analysis_post", side_effect=AssertionError("must not call"),
        ):
            self.daemon._sweep_timer.cancel()
            self.daemon._sweep_queued_analysis()
        saved = self.daemon._session_store.get(stored.id)
        self.assertEqual(saved.outputs["analysis_state"]["notes"]["state"], "blocked")

        self.daemon._set_state(State.RECORDING)
        pending = self._make_lecture_session()
        with patch.object(
            self.daemon, "_analysis_post", return_value=("queued", "Service unreachable.")
        ):
            self.daemon._run_lecture_analysis(pending.id)
        try:
            with patch.object(
                self.daemon, "_analysis_post", side_effect=AssertionError("must not call"),
            ):
                self.daemon._sweep_timer.cancel()
                self.daemon._sweep_queued_analysis()
        finally:
            self.daemon._set_state(State.IDLE)
        saved = self.daemon._session_store.get(pending.id)
        self.assertEqual(saved.outputs["analysis_state"]["notes"]["state"], "queued")

    def test_retry_analysis_validates_session(self) -> None:
        self.assertEqual(self.daemon.command("retry_analysis", {"session_id": ""})["ok"], "false")
        self.assertEqual(self.daemon.command("retry_analysis", {"session_id": "missing"})["ok"], "false")
        stored = self.daemon._session_store.create("dictation")
        self.assertIn(
            "Lecture", self.daemon.command("retry_analysis", {"session_id": stored.id})["error"]
        )

    def test_long_session_persists_timestamped_transcription_segments(self) -> None:
        delivery = FakeDelivery()
        segments = [{
            "start_seconds": 0.25,
            "end_seconds": 1.5,
            "text": "lecture words",
            "speaker_id": None,
        }]
        with patch("transcriber.daemon.make_backend", return_value=FakeBackend()), patch(
            "transcriber.daemon.transcribe_long_audio_with_segments",
            return_value=("lecture words", segments),
        ), patch("transcriber.daemon.make_delivery", return_value=delivery):
            started = self.daemon.command("start_session", {
                "mode": "lecture", "consent_acknowledged": True,
            })
            self.assertEqual(started["state"], "RECORDING")
            session_id = self.daemon._stored_session_id
            self.assertIsNotNone(session_id)
            self.daemon.command("stop_session")
            self.wait_for(State.IDLE)

        assert session_id is not None
        saved = self.daemon._session_store.get(session_id)
        self.assertEqual(saved.source_transcript, "lecture words")
        self.assertEqual(saved.segments, segments)

    def test_processing_failure_enters_error_with_reason(self) -> None:
        with patch("transcriber.daemon.make_backend", side_effect=RuntimeError("boom")):
            self.daemon.command("toggle")
            self.daemon.command("toggle")
            self.wait_for(State.ERROR)
        status = self.daemon.command("status")
        self.assertEqual(status["state"], "ERROR")
        self.assertIn("boom", status["error"])
        # Audio is preserved for retry.
        self.assertTrue((self.data / "last.wav").exists())

    def test_retry_retranscribes_last_audio(self) -> None:
        with patch("transcriber.daemon.make_backend", side_effect=RuntimeError("boom")):
            self.daemon.command("toggle")
            self.daemon.command("toggle")
            self.wait_for(State.ERROR)
        delivery = FakeDelivery()
        with patch("transcriber.daemon.make_backend", return_value=FakeBackend("recovered")), patch(
            "transcriber.daemon.make_delivery", return_value=delivery
        ):
            self.assertEqual(self.daemon.command("retry")["state"], "PROCESSING")
            self.wait_for(State.IDLE)
        self.assertEqual(delivery.inserted, ["recovered"])

    def test_empty_transcript_is_error_not_silent(self) -> None:
        with patch("transcriber.daemon.make_backend", return_value=FakeBackend("   ")), patch(
            "transcriber.daemon.make_delivery", return_value=FakeDelivery()
        ):
            self.daemon.command("toggle")
            self.daemon.command("toggle")
            self.wait_for(State.ERROR)
        self.assertIn("No speech", self.daemon.command("status")["error"])

    def test_empty_transcript_is_soft_error_with_orange_signal(self) -> None:
        with patch("transcriber.daemon.make_backend", return_value=FakeBackend("   ")), patch(
            "transcriber.daemon.make_delivery", return_value=FakeDelivery()
        ):
            self.daemon.command("toggle")
            self.daemon.command("toggle")
            self.wait_for(State.ERROR)
        status = self.daemon.command("status")
        self.assertTrue(status["soft_error"])
        # A fresh run clears the soft flag.
        self.daemon.command("cancel")
        self.assertFalse(self.daemon.command("status")["soft_error"])

    def test_hard_failure_is_not_soft(self) -> None:
        with patch("transcriber.daemon.make_backend", side_effect=RuntimeError("boom")):
            self.daemon.command("toggle")
            self.daemon.command("toggle")
            self.wait_for(State.ERROR)
        status = self.daemon.command("status")
        self.assertIn("boom", status["error"])
        self.assertFalse(status["soft_error"])

    def test_status_reports_timing_backend_and_preview(self) -> None:
        self.daemon.config = Config(ui=UiConfig(show_last_transcript=True, preview_chars=5))
        self.daemon._last_transcript = "hello world"
        self.daemon._last_backend = "groq"
        self.daemon._last_duration = 2.5
        status = self.daemon.command("status")
        self.assertEqual(status["backend"], "local")
        self.assertEqual(status["last_preview"], "hello")
        self.assertEqual(status["last_backend"], "groq")
        self.assertEqual(status["last_duration"], 2.5)

    def test_set_key_persists_secret_and_only_returns_presence(self) -> None:
        keys_file = Path(self.tmp.name) / "keys.env"
        secret = "test-openrouter-secret"
        with patch.dict(os.environ, {"TRANSCRIBER_KEYS_FILE": str(keys_file), "OPENROUTER_API_KEY": ""}):
            response = self.daemon.command("set_key", {"provider": "openrouter", "key": secret})

            self.assertEqual(response["ok"], "true")
            self.assertTrue(response["has_openrouter_key"])
            self.assertNotIn(secret, str(response))
            self.assertEqual(keys_file.read_text(), f"OPENROUTER_API_KEY={secret}\n")
            self.assertEqual(os.environ["OPENROUTER_API_KEY"], secret)

    def test_set_key_clear_and_invalid_input(self) -> None:
        keys_file = Path(self.tmp.name) / "keys.env"
        with patch.dict(os.environ, {
            "TRANSCRIBER_KEYS_FILE": str(keys_file),
            "GROQ_API_KEY": "existing-secret",
        }):
            invalid = self.daemon.command("set_key", {"provider": "local", "key": "secret"})
            malformed = self.daemon.command("set_key", {"provider": "groq", "key": "bad\nINJECTED=value"})
            removed = self.daemon.command("set_key", {"provider": "groq", "clear": True})

            self.assertEqual(invalid["ok"], "false")
            self.assertEqual(malformed["ok"], "false")
            self.assertEqual(removed["ok"], "true")
            self.assertFalse(removed["has_groq_key"])
            self.assertNotIn("GROQ_API_KEY=", keys_file.read_text())

    def test_preview_hidden_by_default(self) -> None:
        self.daemon._last_transcript = "secret text"
        self.assertEqual(self.daemon.command("status")["last_preview"], "")
        self.assertEqual(self.daemon.command("last")["transcript"], "secret text")

    def test_set_backend_rejects_mid_run_and_invalid(self) -> None:
        # Switching to a provider that disables streaming recreates the audio
        # recorder. Keep the fake recorder in place so this state-machine test
        # does not depend on a physical microphone on Windows CI.
        with patch("transcriber.daemon.make_recorder", return_value=self.recorder):
            self.assertEqual(self.daemon.command("set_backend", {"backend": "groq"})["backend"], "groq")
            bad = self.daemon.command("set_backend", {"backend": "nope"})
            self.assertEqual(bad["ok"], "false")
            self.daemon.command("toggle")
            busy = self.daemon.command("set_backend", {"backend": "local"})
            self.assertEqual(busy["ok"], "false")
            self.daemon.command("cancel")

    def test_set_provider_alias_matches_set_backend(self) -> None:
        self.assertEqual(self.daemon.command("set_provider", {"provider": "groq"})["backend"], "groq")
        self.assertEqual(self.daemon.command("set_backend", {"backend": "local"})["backend"], "local")

    def test_set_mic_updates_recorder(self) -> None:
        response = self.daemon.command("set_mic", {"device": "alsa_input.usb"})
        self.assertEqual(response["mic"], "alsa_input.usb")
        self.assertEqual(self.daemon.recorder.config.input_device, "alsa_input.usb")
        self.assertEqual(self.daemon.command("set_mic", {"device": "default"})["mic"], "default")

    def test_mics_command_lists_sources(self) -> None:
        fake = [AudioSource(id="57", name="Built-in Audio", default=True)]
        with patch("transcriber.daemon.list_sources", return_value=fake):
            response = self.daemon.command("mics")
        self.assertEqual(response["ok"], "true")
        self.assertEqual(response["mics"][0]["id"], "57")

    def test_maximum_duration_stops_and_processes(self) -> None:
        delivery = FakeDelivery()
        with patch("transcriber.daemon.make_backend", return_value=FakeBackend()), patch("transcriber.daemon.make_delivery", return_value=delivery):
            self.daemon.command("toggle")
            self.daemon._auto_stop(self.daemon._generation)
            self.wait_for(State.IDLE)
        self.assertEqual(delivery.inserted, ["hello"])

    def test_submit_is_opt_in(self) -> None:
        self.daemon.config = Config(delivery=DeliveryConfig(submit_after_insert=True))
        delivery = FakeDelivery()
        with patch("transcriber.daemon.make_backend", return_value=FakeBackend()), patch("transcriber.daemon.make_delivery", return_value=delivery):
            self.daemon.command("toggle")
            self.daemon.command("toggle")
            self.wait_for(State.IDLE)
        self.assertTrue(delivery.submitted)

    def test_wpctl_parsing_finds_default_source(self) -> None:
        output = "Audio\n ├─ Sources:\n │  *   57. Built-in Audio Analog Stereo        [vol: 0.85]\n │      58. USB Mic                          [vol: 1.00]\n"
        sources = parse_wpctl_status(output)
        self.assertEqual(len(sources), 2)
        self.assertTrue(sources[0].default)
        self.assertEqual(sources[0].id, "57")

    def test_ui_config_parses(self) -> None:
        config = _parse({"ui": {"show_last_transcript": True, "preview_chars": 50}})
        self.assertTrue(config.ui.show_last_transcript)
        self.assertEqual(config.ui.preview_chars, 50)
        self.assertFalse(config.ui.rewrite_enabled)
        self.assertFalse(config.ui.hide_recording_pill)


class DaemonPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.config_path = Path(self.tmp.name) / "config.yaml"
        self.daemon = TranscriberDaemon(Config(), data_dir=self.data, config_path=self.config_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_switches_persist_to_config_file(self) -> None:
        from transcriber.config import load_config

        self.assertEqual(self.daemon.command("set_backend", {"backend": "openrouter"})["backend"], "openrouter")
        self.assertEqual(
            self.daemon.command("set_model", {"model": "openai/whisper-large-v3"})["model"],
            "openai/whisper-large-v3",
        )
        self.assertEqual(self.daemon.command("set_mic", {"device": "usb-mic"})["mic"], "usb-mic")
        reloaded = load_config(self.config_path)
        self.assertEqual(reloaded.transcription.backend, "openrouter")
        self.assertEqual(reloaded.transcription.model, "openai/whisper-large-v3")
        self.assertEqual(reloaded.audio.input_device, "usb-mic")

    def test_rewrite_preference_persists_and_defaults_off(self) -> None:
        from transcriber.config import load_config

        self.assertFalse(self.daemon.command("status")["rewrite_enabled"])
        status = self.daemon.command("set_rewrite", {"enabled": True})
        self.assertEqual(status["ok"], "true")
        self.assertTrue(load_config(self.config_path).ui.rewrite_enabled)
        self.assertEqual(self.daemon.command("set_rewrite", {"enabled": "yes"})["ok"], "false")

    def test_recording_pill_hidden_preference_persists_and_defaults_visible(self) -> None:
        from transcriber.config import load_config

        self.assertFalse(self.daemon.command("status")["hide_recording_pill"])
        status = self.daemon.command("set_recording_pill_hidden", {"hidden": True})
        self.assertEqual(status["ok"], "true")
        self.assertTrue(status["hide_recording_pill"])
        self.assertTrue(load_config(self.config_path).ui.hide_recording_pill)
        self.assertEqual(self.daemon.command("set_recording_pill_hidden", {"hidden": 1})["ok"], "false")

    def test_rewrite_handoff_returns_only_matching_result(self) -> None:
        from transcriber.streaming import LiveSession

        self.daemon._session = LiveSession()
        output: list[str] = []
        worker = threading.Thread(target=lambda: output.append(self.daemon._rewrite_transcript("raw words")))
        worker.start()
        for _ in range(100):
            with self.daemon._lock:
                waiter = self.daemon._rewrite_waiter
            if waiter:
                break
            time.sleep(0.01)
        self.assertIsNotNone(waiter)
        request_id = waiter[0]
        self.assertEqual(self.daemon.command("rewrite_result", {
            "request_id": "not-the-active-id", "text": "wrong",
        })["ok"], "false")
        self.assertEqual(self.daemon.command("rewrite_result", {
            "request_id": request_id, "text": "polished words",
        })["ok"], "true")
        worker.join(timeout=1)
        self.assertEqual(output, ["polished words"])
        events, _ = self.daemon.events_since(0)
        self.assertEqual(events[-1]["type"], "rewrite_request")

    def test_status_reports_key_presence_only(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROQ_API_KEY", None)
            os.environ.pop("OPENROUTER_API_KEY", None)
            status = self.daemon.command("status")
            self.assertFalse(status["has_groq_key"])
            self.assertFalse(status["has_openrouter_key"])
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-x"}, clear=False):
            status = self.daemon.command("status")
            self.assertTrue(status["has_openrouter_key"])
            self.assertNotIn("sk-or-x", json_dumps(status))

    def test_models_local_lists_presets(self) -> None:
        response = self.daemon.command("models")
        self.assertEqual(response["ok"], "true")
        ids = [m["id"] for m in response["models"]]
        self.assertIn("small", ids)
        self.assertTrue(any(m["active"] for m in response["models"] if m["id"] == "base"))

    def test_openai_provider_selects_batch_model_and_live_engine(self) -> None:
        response = self.daemon.command("set_backend", {"backend": "openai"})
        self.assertEqual(response["backend"], "openai")
        self.assertEqual(response["model"], "gpt-transcribe")
        self.assertEqual(response["live_engine"], "openai-realtime")
        models = self.daemon.command("models")
        self.assertIn("gpt-transcribe", [item["id"] for item in models["models"]])

    def test_batch_only_providers_reject_live_toggle(self) -> None:
        for provider in ("groq", "openrouter"):
            self.daemon.command("set_backend", {"backend": provider})
            response = self.daemon.command("set_streaming", {"enabled": True})
            self.assertEqual(response["ok"], "false")
            self.assertIn("batch transcription only", response["error"])
            self.assertFalse(self.daemon.command("status")["streaming"])

    def test_local_provider_selects_local_live_engine(self) -> None:
        self.daemon.command("set_backend", {"backend": "openai"})
        response = self.daemon.command("set_backend", {"backend": "local"})
        self.assertEqual(response["backend"], "local")
        self.assertEqual(response["live_engine"], "local")
        self.assertEqual(response["live_model"], response["model"])

    def test_models_openrouter_prefers_fresh_cache(self) -> None:
        import json as jsonlib
        import time as timelib

        self.daemon.command("set_backend", {"backend": "openrouter"})
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "models-openrouter.json").write_text(
            jsonlib.dumps({"fetched_at": timelib.time(), "models": ["openai/whisper-large-v3"]})
        )
        with patch("transcriber.daemon._fetch_openrouter_models", side_effect=AssertionError("no network")):
            response = self.daemon.command("models")
        self.assertEqual([m["id"] for m in response["models"]], ["openai/whisper-large-v3"])
        self.assertTrue(response["cached"])

    def test_models_openrouter_falls_back_offline(self) -> None:
        self.daemon.command("set_backend", {"backend": "openrouter"})
        with patch("transcriber.daemon._fetch_openrouter_models", side_effect=OSError("offline")):
            response = self.daemon.command("models")
        self.assertIn("openai/whisper-large-v3", [m["id"] for m in response["models"]])
        self.assertFalse(response["cached"])

    def test_models_groq_without_key_uses_fallback(self) -> None:
        import os

        self.daemon.command("set_backend", {"backend": "groq"})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROQ_API_KEY", None)
            response = self.daemon.command("models")
        self.assertIn("whisper-large-v3", [m["id"] for m in response["models"]])


def json_dumps(payload: dict) -> str:
    import json as jsonlib

    return jsonlib.dumps(payload)


class DeliveryChunkingTests(unittest.TestCase):
    def test_long_text_is_typed_in_chunks(self) -> None:
        from transcriber.delivery import KeyboardDelivery

        delivery = KeyboardDelivery.__new__(KeyboardDelivery)
        delivery.wayland = True
        calls: list[list[str]] = []
        with patch("transcriber.delivery.subprocess.run") as run:
            run.return_value = None
            delivery.insert("x" * 450)
            calls = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(calls), 3)
        self.assertEqual("".join(chunk[-1] for chunk in calls), "x" * 450)
        self.assertTrue(all(chunk[:3] == ["ydotool", "type", "--"] for chunk in calls))

    def test_short_text_is_single_call(self) -> None:
        from transcriber.delivery import KeyboardDelivery

        delivery = KeyboardDelivery.__new__(KeyboardDelivery)
        delivery.wayland = True
        with patch("transcriber.delivery.subprocess.run") as run:
            run.return_value = None
            delivery.insert("hi")
            self.assertEqual(run.call_count, 1)


@unittest.skipIf(os.name == "nt", "Wayland detection is Linux-specific")
class WaylandDetectionTests(unittest.TestCase):
    def test_env_vars_select_ydotool(self) -> None:
        from transcriber.delivery import _detect_wayland

        with patch.dict(os.environ, {"XDG_SESSION_TYPE": "wayland"}, clear=False):
            self.assertTrue(_detect_wayland())
        with patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}, clear=False):
            # XDG_SESSION_TYPE may still read wayland here; force x11 + socket.
            with patch.dict(os.environ, {"XDG_SESSION_TYPE": "x11"}, clear=False):
                self.assertTrue(_detect_wayland())

    def test_compositor_socket_is_fallback_signal(self) -> None:
        import tempfile
        from pathlib import Path

        from transcriber.delivery import _detect_wayland

        with tempfile.TemporaryDirectory() as directory:
            fake_socket = Path(directory) / "wayland-9"
            env = {"XDG_SESSION_TYPE": "x11", "WAYLAND_DISPLAY": "", "XDG_RUNTIME_DIR": directory}
            with patch.dict(os.environ, env, clear=False):
                os.environ.pop("WAYLAND_DISPLAY", None)
                self.assertFalse(_detect_wayland())
                fake_socket.touch()
                with patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-9"}, clear=False):
                    self.assertTrue(_detect_wayland())
