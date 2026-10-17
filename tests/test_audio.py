from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from transcriber.audio import SoxRecorder, pcm_audio_level, pcm_audio_waveform, repair_wav_header
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


class WavRepairTests(unittest.TestCase):
    def _write_crash_file(self, path, frames: bytes) -> None:
        import struct
        import wave

        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(frames)
        # Simulate a kill mid-capture: append frames the header never learned.
        with open(path, "r+b") as stream:
            stream.seek(0, 2)
            stream.write(frames)
            stream.truncate()

    def test_repair_restores_crashed_frames(self) -> None:
        import struct
        import tempfile
        import wave
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            frames = b"\x01\x02" * 8000
            self._write_crash_file(path, frames)
            with wave.open(str(path), "rb") as before:
                claimed = before.getnframes()
            self.assertEqual(claimed, 8000)
            self.assertTrue(repair_wav_header(path))
            with wave.open(str(path), "rb") as after:
                self.assertEqual(after.getnframes(), 16000)
                self.assertEqual(after.readframes(16000), frames + frames)

    def test_repair_leaves_consistent_files_alone(self) -> None:
        import tempfile
        import wave
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b"\x01\x02" * 100)
            before = path.read_bytes()
            self.assertFalse(repair_wav_header(path))
            self.assertEqual(path.read_bytes(), before)

    def test_repair_rejects_non_wav(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            path.write_bytes(b"not a wav file at all..............")
            self.assertFalse(repair_wav_header(path))
            self.assertFalse(repair_wav_header(Path(directory) / "missing.wav"))
