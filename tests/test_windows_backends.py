from __future__ import annotations

import sys
import unittest
import ctypes
import threading
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from transcriber.audio import AudioError, AudioSource, make_recorder
from transcriber.config import AudioConfig, Config, DeliveryConfig
from transcriber.delivery import _detect_wayland, make_delivery
from transcriber.platforms.windows.audio import (
    WindowsRecorder,
    list_sources as windows_list_sources,
    pcm_audio_level,
)
from transcriber.platforms.windows.delivery import (
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    WindowsKeyboardDelivery,
    WindowsClipboardDelivery,
    _INPUT,
)


class WindowsBackendTests(unittest.TestCase):
    def test_pcm_audio_level_maps_silence_and_voice_to_bounded_meter_values(self) -> None:
        silence = bytes(640)
        quiet_voice = b"\x00\x10" * 320
        loud_voice = b"\x00\x40" * 320

        self.assertEqual(pcm_audio_level(silence), 0.0)
        quiet_level = pcm_audio_level(quiet_voice)
        loud_level = pcm_audio_level(loud_voice)
        self.assertGreater(quiet_level, 0.0)
        self.assertGreater(loud_level, quiet_level)
        self.assertLessEqual(loud_level, 1.0)

    def test_shared_audio_factory_selects_windows_recorder(self) -> None:
        with patch("transcriber.audio.os.name", "nt"):
            recorder = make_recorder(AudioConfig())
        self.assertIsInstance(recorder, WindowsRecorder)

    def test_shared_delivery_factory_selects_windows_backend(self) -> None:
        with patch("transcriber.delivery.os.name", "nt"), patch(
            "transcriber.platforms.windows.delivery._windows_api", return_value=(MagicMock(), MagicMock())
        ):
            delivery = make_delivery(DeliveryConfig())
        self.assertIsInstance(delivery, WindowsKeyboardDelivery)

    def test_windows_never_probes_linux_wayland_paths(self) -> None:
        with patch("transcriber.delivery.os.name", "nt"), patch.dict(
            "os.environ", {"XDG_SESSION_TYPE": "", "WAYLAND_DISPLAY": "", "XDG_RUNTIME_DIR": ""}
        ):
            self.assertFalse(_detect_wayland())

    def test_unicode_delivery_emits_utf16_key_events_including_surrogate_pair(self) -> None:
        user32 = MagicMock()
        user32.SendInput.side_effect = lambda count, events, _size: count
        delivery = WindowsKeyboardDelivery.__new__(WindowsKeyboardDelivery)
        delivery.user32 = user32

        delivery.insert("A😀")

        count, events, _size = user32.SendInput.call_args.args
        scans = [events[index].ki.wScan for index in range(count) if index % 2 == 0]
        flags = [events[index].ki.dwFlags for index in range(count)]
        self.assertEqual(scans, [ord("A"), 0xD83D, 0xDE00])
        self.assertEqual(flags, [KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP] * 3)

    @unittest.skipUnless(sys.platform == "win32", "Windows API binding smoke test")
    def test_native_input_and_clipboard_api_bindings_load(self) -> None:
        self.assertEqual(ctypes.sizeof(_INPUT), 40)
        WindowsKeyboardDelivery()
        WindowsClipboardDelivery()

    def test_recorder_writes_callback_audio_to_a_valid_wav(self) -> None:
        class FakeStream:
            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]

            def start(self):
                self.callback(bytes(640), 320, None, None)

            def stop(self):
                pass

            def close(self):
                pass

        with patch.dict(sys.modules, {"sounddevice": MagicMock(InputStream=FakeStream)}):
            recorder = WindowsRecorder(AudioConfig(), frame_ms=20)
            path = recorder.start()
            self.assertTrue(recorder.live_available)
            frame = recorder.read_frame(timeout=1)
            self.assertEqual(frame, bytes(640))
            self.assertEqual(recorder.window_bytes(), bytes(640))
            self.assertEqual(recorder.audio_level, 0.0)
            wav_path = recorder.stop()
        try:
            self.assertEqual(path, wav_path)
            with open(wav_path, "rb") as audio_file:
                header = audio_file.read(44)
            self.assertEqual(header[:4], b"RIFF")
            self.assertEqual(header[8:12], b"WAVE")
            self.assertEqual(recorder.captured_seconds(), 0.02)
        finally:
            wav_path.unlink(missing_ok=True)

    def test_recorder_exposes_level_measured_from_captured_pcm(self) -> None:
        pcm = b"\x00\x40" * 320

        class FakeStream:
            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]

            def start(self):
                self.callback(pcm, 320, None, None)

            def stop(self):
                pass

            def close(self):
                pass

        with patch.dict(sys.modules, {"sounddevice": MagicMock(InputStream=FakeStream)}):
            recorder = WindowsRecorder(AudioConfig(), frame_ms=20)
            recorder.start()
            self.assertEqual(recorder.read_frame(timeout=1), pcm)
            level = recorder.audio_level
            wav_path = recorder.stop()
        try:
            self.assertGreater(level, 0.0)
            self.assertLessEqual(level, 1.0)
        finally:
            wav_path.unlink(missing_ok=True)

    def test_recorder_constructor_failure_cleans_up_without_masking_device_error(self) -> None:
        fake_sounddevice = MagicMock(
            InputStream=MagicMock(side_effect=RuntimeError("no input device"))
        )
        with patch.dict(sys.modules, {"sounddevice": fake_sounddevice}):
            recorder = WindowsRecorder(AudioConfig())
            with self.assertRaisesRegex(AudioError, "Could not open Windows microphone: no input device"):
                recorder.start()

        self.assertIsNone(recorder._writer)
        self.assertIsNone(recorder._path)

    def test_microphone_enumeration_marks_default_and_ignores_output_only(self) -> None:
        sounddevice = MagicMock(
            default=SimpleNamespace(device=(1, 1)),
            query_devices=MagicMock(return_value=[
                {"name": "Speakers", "max_input_channels": 0},
                {"name": "USB microphone", "max_input_channels": 2},
            ]),
        )
        with patch.dict(sys.modules, {"sounddevice": sounddevice}):
            sources = windows_list_sources()
        self.assertEqual(sources, [AudioSource(id="1", name="USB microphone", default=True)])

    def test_stop_error_still_closes_stream_and_joins_writer(self) -> None:
        class BrokenStream:
            closed = False
            aborted = False

            def start(self):
                pass

            def stop(self):
                raise RuntimeError("device removed")

            def abort(self):
                self.aborted = True

            def close(self):
                self.closed = True

        stream = BrokenStream()
        fake_sounddevice = MagicMock(InputStream=MagicMock(return_value=stream))
        with patch.dict(sys.modules, {"sounddevice": fake_sounddevice}):
            recorder = WindowsRecorder(AudioConfig())
            audio_path = recorder.start()
            with self.assertRaisesRegex(AudioError, "did not stop cleanly"):
                recorder.stop()
        self.assertTrue(stream.aborted)
        self.assertTrue(stream.closed)
        self.assertIsNone(recorder._writer)
        self.assertFalse(audio_path.exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows named-pipe smoke test")
    def test_windows_named_pipe_server_handles_commands_and_shutdown(self) -> None:
        from transcriber.platforms.windows.ipc import WindowsPipeServer, request

        class FakeDaemon:
            def command(self, command, _params):
                return {"ok": "true", "state": "IDLE", "command": command}

        server = WindowsPipeServer(FakeDaemon())
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        try:
            response = request({"command": "status"})
            self.assertEqual(response["state"], "IDLE")
            response = request({"command": "shutdown"})
            self.assertEqual(response["state"], "STOPPING")
            serving.join(timeout=5)
            self.assertFalse(serving.is_alive())
        finally:
            if serving.is_alive():
                server.shutdown()
                serving.join(timeout=5)
            server.server_close()

    @unittest.skipUnless(sys.platform == "win32", "Windows named-pipe adoption integration test")
    def test_sidecar_adopts_an_existing_daemon_without_replacing_its_pipe(self) -> None:
        from transcriber.daemon import DaemonAlreadyRunning, default_socket_path, run_server
        from transcriber.platforms.windows.ipc import WindowsPipeServer

        class FakeDaemon:
            def command(self, command, _params):
                return {"ok": "true", "state": "IDLE", "command": command}

        server = WindowsPipeServer(FakeDaemon())
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        try:
            with self.assertRaises(DaemonAlreadyRunning):
                run_server(Config(), default_socket_path(), ui_token="per-user-token")
            self.assertEqual(server.daemon.command("status", {})["state"], "IDLE")
        finally:
            server.shutdown()
            serving.join(timeout=5)
            server.server_close()
