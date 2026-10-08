from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from transcriber.audio import SoxRecorder
from transcriber.config import AudioConfig


class AudioTests(unittest.TestCase):
    def test_sox_capture_is_normalized_and_stop_finalizes_wav(self) -> None:
        process = MagicMock()
        with patch("transcriber.audio.subprocess.Popen", return_value=process) as popen:
            recorder = SoxRecorder(AudioConfig())
            audio = recorder.start()
            audio.write_bytes(b"R" * 45)
            self.assertEqual(recorder.stop(), audio)
            audio.unlink()
        command = popen.call_args.args[0]
        self.assertEqual(command[:8], ["rec", "-q", "-r", "16000", "-c", "1", "-b", "16"])
        process.send_signal.assert_called_once()

    def test_cancel_removes_temporary_audio(self) -> None:
        process = MagicMock()
        with patch("transcriber.audio.subprocess.Popen", return_value=process):
            recorder = SoxRecorder(AudioConfig())
            audio = recorder.start()
            audio.write_bytes(b"temporary audio")
            recorder.cancel()
            self.assertFalse(audio.exists())
