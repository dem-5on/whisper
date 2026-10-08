from __future__ import annotations

import queue
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from transcriber.audio import AudioConfig, SoxRecorder, StreamingRecorder, make_recorder
from transcriber.config import Config, ConfigurationError, StreamingConfig, _parse
from transcriber.daemon import TranscriberDaemon
from transcriber.state import State
from transcriber.streaming import LiveSession, RollingBuffer, reconcile, stable_prefix, suffix_prefix_overlap
from transcriber.transcription import FasterWhisperBackend, clear_model_cache
from transcriber.vad import EnergyVad, SpeechEndpoint, frame_rms_dbfs, sensitivity_to_threshold_dbfs


def loud_frame(n_samples: int = 800) -> bytes:
    return struct.pack(f"<{n_samples}h", *([20000] * n_samples))


def quiet_frame(n_samples: int = 800) -> bytes:
    return b"\x00" * (n_samples * 2)


class VadTests(unittest.TestCase):
    def test_threshold_mapping_spans_expected_range(self) -> None:
        self.assertAlmostEqual(sensitivity_to_threshold_dbfs(0.0), -25.0)
        self.assertAlmostEqual(sensitivity_to_threshold_dbfs(1.0), -45.0)
        mid = sensitivity_to_threshold_dbfs(0.5)
        self.assertLess(-45.0, mid)
        self.assertLess(mid, -25.0)

    def test_rms_distinguishes_loud_from_silence(self) -> None:
        self.assertGreater(frame_rms_dbfs(loud_frame()), -10.0)
        self.assertEqual(frame_rms_dbfs(quiet_frame()), float("-inf"))
        self.assertEqual(frame_rms_dbfs(b""), float("-inf"))

    def test_energy_vad_labels_frames(self) -> None:
        vad = EnergyVad(sensitivity=0.5)
        self.assertTrue(vad.is_speech(loud_frame()))
        quiet_vad = EnergyVad(sensitivity=0.5)
        self.assertFalse(quiet_vad.is_speech(quiet_frame()))

    def test_endpoint_requires_speech_before_autostop(self) -> None:
        endpoint = SpeechEndpoint(silence_timeout_seconds=0.5)
        endpoint.update(False, now=100.0)
        endpoint.update(False, now=100.4)
        self.assertFalse(endpoint.should_auto_stop(now=105.0))

    def test_endpoint_autostops_after_trailing_silence(self) -> None:
        endpoint = SpeechEndpoint(silence_timeout_seconds=0.5)
        self.assertEqual(endpoint.update(True, now=100.0), "speech_started")
        self.assertEqual(endpoint.update(False, now=100.1), "speech_ended")
        self.assertFalse(endpoint.should_auto_stop(now=100.3))
        self.assertTrue(endpoint.should_auto_stop(now=100.7))

    def test_endpoint_ignores_leading_silence(self) -> None:
        endpoint = SpeechEndpoint(silence_timeout_seconds=0.5)
        for step in range(10):
            endpoint.update(False, now=100.0 + step * 0.2)
        self.assertFalse(endpoint.should_auto_stop(now=110.0))


class StablePrefixTests(unittest.TestCase):
    def test_common_prefix(self) -> None:
        self.assertEqual(stable_prefix(["a", "b"], ["a", "b", "c"]), ["a", "b"])
        self.assertEqual(stable_prefix(["a", "x"], ["a", "y"]), ["a"])
        self.assertEqual(stable_prefix(["a"], ["b"]), [])

    def test_reconcile_never_shrinks_committed(self) -> None:
        # A hypothesis that revises inside the committed region must not
        # rewrite stable text; the conflict resolves at finalization.
        committed, provisional = reconcile(["hello", "world"], ["hello", "world"], ["hello", "there"])
        self.assertEqual(committed, ["hello", "world"])
        self.assertEqual(provisional, [])

    def test_reconcile_grows_on_agreement(self) -> None:
        committed, provisional = reconcile([], [], ["hello", "world"])
        self.assertEqual(committed, [])
        self.assertEqual(provisional, ["hello", "world"])
        committed, provisional = reconcile(committed, ["hello", "world"], ["hello", "world", "again"])
        self.assertEqual(committed, ["hello", "world"])
        self.assertEqual(provisional, ["again"])

    def test_session_final_replaces_provisional(self) -> None:
        session = LiveSession("abc123")
        session.push_hypothesis("hello world")
        self.assertEqual(session.live_text, "hello world")
        event = session.finalize("hello world!")
        self.assertEqual(event.type, "final")
        self.assertEqual(session.live_text, "hello world!")
        self.assertEqual(session.provisional_text, "")

    def test_session_events_carry_id_and_revision(self) -> None:
        session = LiveSession("sess42")
        first = session.push_hypothesis("hi")
        second = session.push_hypothesis("hi there")
        self.assertEqual((first.session_id, second.session_id), ("sess42", "sess42"))
        self.assertLess(first.revision, second.revision)
        session.note_speech(True)
        self.assertTrue(session.speech_active)

    def test_session_preserves_text_when_rolling_window_advances(self) -> None:
        session = LiveSession("sess42")
        session.push_hypothesis("one two three four")
        session.push_hypothesis("one two three four five")
        # The next 4-word audio window has discarded "one", but overlaps
        # the previous hypothesis. The old prefix must remain and only the
        # genuinely new tail may be appended.
        session.push_hypothesis("two three four five six")
        self.assertEqual(session.live_text, "one two three four five six")
        self.assertEqual(suffix_prefix_overlap(["a", "b", "c"], ["b", "c", "d"]), 2)

    def test_rolling_buffer_bounds_memory(self) -> None:
        buffer = RollingBuffer(window_seconds=4)
        chunk = loud_frame(800)  # 0.05 s
        for _ in range(400):  # 20 s of audio into a 4 s window
            buffer.append(chunk)
        self.assertLessEqual(len(buffer.window_bytes()), 4 * 16000 * 2 + len(chunk))
        self.assertAlmostEqual(buffer.duration_seconds(), 4.0, delta=0.2)
        samples = buffer.window_samples()
        self.assertIsNotNone(samples)
        assert samples is not None
        self.assertGreater(len(samples), 0)


class StreamingConfigTests(unittest.TestCase):
    def test_defaults_enable_live_pipeline(self) -> None:
        config = _parse({})
        self.assertTrue(config.streaming.enabled)
        self.assertEqual(config.streaming.frame_ms, 20)
        self.assertEqual(config.streaming.chunk_ms, 500)
        self.assertEqual(config.streaming.window_seconds, 8)
        self.assertFalse(config.streaming.live_insert_experimental)

    def test_legacy_configs_without_streaming_still_parse(self) -> None:
        config = _parse({"transcription": {"backend": "groq"}})
        self.assertTrue(config.streaming.enabled)
        self.assertEqual(config.transcription.backend, "groq")

    def test_invalid_streaming_values_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            _parse({"streaming": {"frame_ms": 5}})
        with self.assertRaises(ConfigurationError):
            _parse({"streaming": {"chunk_ms": 50}})
        with self.assertRaises(ConfigurationError):
            _parse({"streaming": {"silence_timeout_seconds": 30}})
        with self.assertRaises(ConfigurationError):
            _parse({"streaming": {"vad_sensitivity": 2.0}})

    def test_streaming_roundtrips_through_save_load(self) -> None:
        from transcriber.config import load_config, save_config

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            save_config(Config(streaming=StreamingConfig(chunk_ms=750)), path)
            self.assertEqual(load_config(path).streaming.chunk_ms, 750)

    def test_openai_live_engine_config_is_independent_of_batch_provider(self) -> None:
        config = _parse({
            "transcription": {"backend": "openrouter", "model": "openai/whisper-large-v3"},
            "streaming": {"engine": "openai-realtime", "model": "gpt-live-transcribe"},
        })
        self.assertEqual(config.transcription.backend, "openrouter")
        self.assertEqual(config.streaming.engine, "openai-realtime")
        self.assertEqual(config.streaming.model, "gpt-live-transcribe")

    def test_invalid_live_engine_model_pair_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            _parse({"streaming": {"engine": "openai-realtime", "model": "base"}})


class StreamingRecorderTests(unittest.TestCase):
    def test_make_recorder_prefers_streaming(self) -> None:
        self.assertIsInstance(make_recorder(AudioConfig()), StreamingRecorder)
        self.assertIsInstance(make_recorder(AudioConfig(), streaming_enabled=False), SoxRecorder)

    def test_streaming_frames_and_wav_finalize(self) -> None:
        process = MagicMock()
        process.stdout.read.side_effect = [loud_frame(), quiet_frame(), b""]
        with patch("transcriber.audio.subprocess.Popen", return_value=process):
            recorder = StreamingRecorder(AudioConfig(), frame_ms=50, max_queue_seconds=30)
            reserved = recorder.start()
            try:
                first = recorder.read_frame(timeout=2)
                second = recorder.read_frame(timeout=2)
                self.assertEqual(first, loud_frame())
                self.assertEqual(second, quiet_frame())
                self.assertGreater(len(recorder.window_bytes()), 0)
                final = recorder.stop()
                self.assertTrue(final.exists())
                self.assertGreater(final.stat().st_size, 44)
            finally:
                reserved.unlink(missing_ok=True)
        command = process.send_signal.call_args  # SIGINT gracefully drains raw stdout
        self.assertIsNotNone(command)

    def test_bounded_queue_drops_oldest(self) -> None:
        process = MagicMock()
        process.stdout.read.side_effect = [loud_frame()] * 40 + [b""]
        with patch("transcriber.audio.subprocess.Popen", return_value=process):
            # ~0.4 s of buffering at 50 ms frames.
            recorder = StreamingRecorder(AudioConfig(), frame_ms=50, max_queue_seconds=1)
            reserved = recorder.start()
            try:
                recorder._pump()
                self.assertLessEqual(recorder._frames.qsize(), 20 + 1)
            finally:
                recorder.cancel()
                reserved.unlink(missing_ok=True)


class ModelCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_model_cache()

    def tearDown(self) -> None:
        clear_model_cache()

    def test_model_is_shared_across_backend_instances(self) -> None:
        import sys
        from types import ModuleType

        fake_model = MagicMock()
        fake_model.transcribe.return_value = ([MagicMock(text="hi")], None)
        module = ModuleType("faster_whisper")
        module.WhisperModel = MagicMock(return_value=fake_model)  # type: ignore[attr-defined]
        with patch.dict(sys.modules, {"faster_whisper": module}):
            first = FasterWhisperBackend("tiny")
            second = FasterWhisperBackend("tiny")
            first.ensure_loaded()
            second.ensure_loaded()
            self.assertEqual(module.WhisperModel.call_count, 1)
            self.assertIs(first.ensure_loaded(), second.ensure_loaded())

    def test_different_keys_load_separately(self) -> None:
        import sys
        from types import ModuleType

        module = ModuleType("faster_whisper")
        module.WhisperModel = MagicMock(side_effect=[MagicMock(), MagicMock()])  # type: ignore[attr-defined]
        with patch.dict(sys.modules, {"faster_whisper": module}):
            FasterWhisperBackend("tiny").ensure_loaded()
            FasterWhisperBackend("base").ensure_loaded()
            self.assertEqual(module.WhisperModel.call_count, 2)


class FakeStreamingRecorder:
    """Deterministic streaming recorder: pre-fed PCM frames, no subprocess."""

    def __init__(self, frames: list[bytes], audio: Path) -> None:
        self.config = AudioConfig()
        self._frames: queue.Queue[bytes] = queue.Queue()
        self._all = b"".join(frames)
        for frame in frames:
            self._frames.put(frame)
        self._audio = audio
        self.cancelled = False

    def start(self) -> Path:
        return self._audio

    def read_frame(self, timeout: float | None = None) -> bytes | None:
        try:
            return self._frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def window_bytes(self, max_seconds: int = 30) -> bytes:
        return self._all[-max_seconds * 16000 * 2 :]

    def stop(self) -> Path:
        self._audio.write_bytes(b"wav" * 20)
        return self._audio

    def cancel(self) -> None:
        self.cancelled = True
        self._audio.unlink(missing_ok=True)


class FakeBackend:
    def __init__(self, text: str = "hello") -> None:
        self.text = text

    def transcribe(self, _audio: Path) -> str:
        return self.text


class FakeDelivery:
    def __init__(self) -> None:
        self.inserted: list[str] = []

    def insert(self, text: str) -> None:
        self.inserted.append(text)

    def submit(self) -> None:
        pass


class DaemonLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.audio = Path(self.tmp.name) / "audio.wav"
        self.data = Path(self.tmp.name) / "data"
        config = Config()
        self.daemon = TranscriberDaemon(config, data_dir=self.data)

    def tearDown(self) -> None:
        try:
            self.daemon.shutdown()
        finally:
            self.tmp.cleanup()

    def wait_for(self, *states: State, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.daemon.state in states:
                return
            time.sleep(0.02)
        self.fail(f"daemon did not reach {states}; at {self.daemon.state}")

    def test_toggle_creates_live_session_visible_in_status(self) -> None:
        self.daemon.recorder = FakeStreamingRecorder([], self.audio)  # type: ignore[assignment]
        with patch("transcriber.daemon.make_backend", return_value=FakeBackend()), patch(
            "transcriber.daemon.make_delivery", return_value=FakeDelivery()
        ):
            status = self.daemon.command("toggle")
            try:
                self.assertEqual(status["state"], "RECORDING")
                self.assertTrue(status["session_id"])
                self.assertEqual(status["live_revision"], 0)
                self.daemon._push_live_event(status["session_id"], "partial", "hello world")
                live = self.daemon.command("status")
                self.assertEqual(live["live_text"], "hello world")
                self.assertGreater(live["live_revision"], 0)
            finally:
                self.daemon.command("cancel")

    def test_groq_provider_is_batch_only_even_if_local_live_engine_is_configured(self) -> None:
        import dataclasses

        transcription = dataclasses.replace(self.daemon.config.transcription, backend="groq")
        self.daemon.config = dataclasses.replace(self.daemon.config, transcription=transcription)
        status = self.daemon.command("status")
        self.assertFalse(status["live_available"])
        self.assertIn("batch transcription only", status["live_unavailable_reason"])
        self.assertEqual(status["live_engine"], "local")

    def test_events_command_returns_session_revisions(self) -> None:
        self.daemon.recorder = FakeStreamingRecorder([], self.audio)  # type: ignore[assignment]
        with patch("transcriber.daemon.make_backend", return_value=FakeBackend()), patch(
            "transcriber.daemon.make_delivery", return_value=FakeDelivery()
        ):
            started = self.daemon.command("toggle")
            try:
                session_id = started["session_id"]
                self.daemon._push_live_event(session_id, "partial", "hello")
                self.daemon._push_live_event(session_id, "partial", "hello world")
                response = self.daemon.command("events", {"since": 0})
                self.assertEqual(response["ok"], "true")
                self.assertEqual(len(response["events"]), 2)
                self.assertTrue(all(e["session_id"] == session_id for e in response["events"]))
                revisions = [e["revision"] for e in response["events"]]
                self.assertEqual(revisions, sorted(revisions))
                followup = self.daemon.command("events", {"since": revisions[-1]})
                self.assertEqual(followup["events"], [])
                filtered = self.daemon.command("events", {"since": 0, "session_id": "other"})
                self.assertEqual(filtered["events"], [])
            finally:
                self.daemon.command("cancel")

    def test_final_transcript_reconciles_and_delivers_only_final(self) -> None:
        self.daemon.recorder = FakeStreamingRecorder([], self.audio)  # type: ignore[assignment]
        delivery = FakeDelivery()
        with patch("transcriber.daemon.make_backend", return_value=FakeBackend("final text")), patch(
            "transcriber.daemon.make_delivery", return_value=delivery
        ):
            started = self.daemon.command("toggle")
            self.daemon._push_live_event(started["session_id"], "partial", "provisional words")
            self.daemon.command("toggle")
            self.wait_for(State.IDLE)
        self.assertEqual(delivery.inserted, ["final text"])
        self.assertEqual(self.daemon._session.live_text, "final text")

    def test_vad_silence_autostops_recording(self) -> None:
        import dataclasses

        streaming = dataclasses.replace(
            self.daemon.config.streaming, silence_timeout_seconds=0.5, chunk_ms=200,
        )
        self.daemon.config = dataclasses.replace(self.daemon.config, streaming=streaming)
        frames = [loud_frame()] * 6 + [quiet_frame()] * 6
        self.daemon.recorder = FakeStreamingRecorder(frames, self.audio)  # type: ignore[assignment]
        delivery = FakeDelivery()
        # Backend without transcribe_samples: decode loop exits, VAD still runs.
        with patch("transcriber.daemon.make_backend", return_value=FakeBackend("auto stopped")), patch(
            "transcriber.daemon.make_delivery", return_value=delivery
        ):
            self.assertEqual(self.daemon.command("toggle")["state"], "RECORDING")
            # No manual stop: trailing silence must end the recording.
            self.wait_for(State.IDLE, timeout=15.0)
        self.assertEqual(delivery.inserted, ["auto stopped"])


class DaemonStreamingSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.config_path = Path(self.tmp.name) / "config.yaml"
        self.daemon = TranscriberDaemon(Config(), data_dir=self.data, config_path=self.config_path)

    def tearDown(self) -> None:
        try:
            self.daemon.shutdown()
        finally:
            self.tmp.cleanup()

    def test_set_streaming_disables_and_rebuilds_recorder(self) -> None:
        from transcriber.audio import SoxRecorder, StreamingRecorder
        from transcriber.config import load_config

        self.assertIsInstance(self.daemon.recorder, StreamingRecorder)
        response = self.daemon.command("set_streaming", {"enabled": False})
        self.assertEqual(response["ok"], "true")
        self.assertFalse(response["streaming"])
        self.assertIsInstance(self.daemon.recorder, SoxRecorder)
        self.assertFalse(load_config(self.config_path).streaming.enabled)
        response = self.daemon.command("set_streaming", {"enabled": "true"})
        self.assertTrue(response["streaming"])
        self.assertIsInstance(self.daemon.recorder, StreamingRecorder)
        self.assertTrue(load_config(self.config_path).streaming.enabled)

    def test_live_engine_and_model_switch_independently(self) -> None:
        from transcriber.config import load_config

        response = self.daemon.command("set_live_engine", {"engine": "openai-realtime"})
        self.assertEqual(response["ok"], "true")
        self.assertEqual(response["backend"], "local")
        self.assertEqual(response["live_engine"], "openai-realtime")
        self.assertEqual(response["live_model"], "gpt-live-transcribe")
        response = self.daemon.command("set_live_model", {"model": "gpt-transcribe"})
        self.assertEqual(response["live_model"], "gpt-transcribe")
        saved = load_config(self.config_path)
        self.assertEqual(saved.transcription.backend, "local")
        self.assertEqual(saved.streaming.model, "gpt-transcribe")
        self.assertEqual(self.daemon.command("set_live_model", {"model": "base"})["ok"], "false")

    def test_set_streaming_without_value_flips(self) -> None:
        self.assertFalse(self.daemon.command("set_streaming")["streaming"])
        self.assertTrue(self.daemon.command("set_streaming", {})["streaming"])

    def test_openrouter_blocks_live_transcription_with_clear_message(self) -> None:
        self.daemon.command("set_backend", {"backend": "openrouter"})
        status = self.daemon.command("status")
        self.assertFalse(status["live_available"])
        self.assertFalse(status["streaming"])
        # `streaming` is a capture/VAD setting and can be off independently;
        # enabling live transcription through the command is rejected.
        self.daemon.command("set_streaming", {"enabled": False})
        rejected = self.daemon.command("set_streaming", {"enabled": True})
        self.assertEqual(rejected["ok"], "false")
        self.assertIn("Select Local or OpenAI", rejected["error"])

    def test_set_streaming_rejects_invalid_and_mid_run(self) -> None:
        bad = self.daemon.command("set_streaming", {"enabled": "maybe"})
        self.assertEqual(bad["ok"], "false")
        self.assertTrue(bad["streaming"])  # unchanged
        self.daemon.recorder = FakeStreamingRecorder([], Path(self.tmp.name) / "a.wav")  # type: ignore[assignment]
        with patch("transcriber.daemon.make_backend", return_value=FakeBackend()), patch(
            "transcriber.daemon.make_delivery", return_value=FakeDelivery()
        ):
            self.daemon.command("toggle")
            try:
                busy = self.daemon.command("set_streaming", {"enabled": False})
                self.assertEqual(busy["ok"], "false")
                self.assertIn("mid-run", busy["error"])
            finally:
                self.daemon.command("cancel")


if __name__ == "__main__":
    unittest.main()
