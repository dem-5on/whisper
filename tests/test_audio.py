from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from transcriber.audio import SoxRecorder, pcm_audio_level, pcm_audio_waveform
from transcriber.config import AudioConfig


class AudioTests(unittest.TestCase):
    def test_pcm_audio_meter_and_waveform_reflect_sampled_audio(self) -> None:
        import struct

        silence = bytes(32)
        signal = struct.pack("<8h", 0, 1000, -2000, 3000, -4000, 5000, -6000, 7000)
        self.assertEqual(pcm_audio_level(silence), 0.0)
        self.assertGreater(pcm_audio_level(signal), 0.0)
        waveform = pcm_audio_waveform(signal, buckets=4)
        self.assertEqual(len(waveform), 8)
        self.assertAlmostEqual(waveform[0], 0.0)
        self.assertAlmostEqual(waveform[1], 1000 / 32768)
        self.assertAlmostEqual(waveform[-1], 7000 / 32768)

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
