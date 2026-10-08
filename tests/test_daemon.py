from __future__ import annotations

from pathlib import Path
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from transcriber.audio import AudioSource, parse_wpctl_status
from transcriber.config import Config, DeliveryConfig, UiConfig, _parse
from transcriber.daemon import TranscriberDaemon
from transcriber.state import State


class FakeRecorder:
    def __init__(self, audio: Path) -> None:
        self.audio = audio
        self.started = False
        self.cancelled = False
        self.config = None

    def start(self) -> Path:
        self.started = True
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

    def test_cancel_discards_recording_without_transcribing(self) -> None:
        with patch("transcriber.daemon.make_backend") as backend:
            self.daemon.command("toggle")
            response = self.daemon.command("cancel")
        self.assertEqual(response["state"], "IDLE")
        self.assertTrue(self.recorder.cancelled)
        backend.assert_not_called()

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

    def test_preview_hidden_by_default(self) -> None:
        self.daemon._last_transcript = "secret text"
        self.assertEqual(self.daemon.command("status")["last_preview"], "")
        self.assertEqual(self.daemon.command("last")["transcript"], "secret text")

    def test_set_backend_rejects_mid_run_and_invalid(self) -> None:
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
