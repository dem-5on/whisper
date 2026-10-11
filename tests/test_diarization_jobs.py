from __future__ import annotations

import asyncio
import io
from pathlib import Path
import tempfile
import unittest
import wave

from whisper_service.diarization_api import _validate_wav
from whisper_service.diarization_jobs import DiarizationJobManager
from whisper_service.sessions import ServiceError


def wav_bytes(*, channels: int = 1, sample_width: int = 2, sample_rate: int = 16_000,
              frames: int = 160) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\0" * frames * channels * sample_width)
    return output.getvalue()


class DiarizationUploadValidationTests(unittest.TestCase):
    def test_accepts_expected_pcm_wav_and_rejects_other_audio_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "valid.wav"
            audio.write_bytes(wav_bytes())
            _validate_wav(audio)
            audio.write_bytes(wav_bytes(channels=2))
            with self.assertRaisesRegex(ValueError, "mono PCM16"):
                _validate_wav(audio)
            audio.write_bytes(b"not a wav")
            with self.assertRaisesRegex(ValueError, "valid WAV"):
                _validate_wav(audio)


class DiarizationJobManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_is_user_scoped_and_audio_is_deleted_after_processing(self) -> None:
        class Worker:
            async def diarize(self, _audio):
                return [{"start_seconds": 0.1, "end_seconds": 0.5, "speaker_id": "speaker_1"}]

        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "upload.wav"
            audio.write_bytes(wav_bytes())
            manager = DiarizationJobManager(Worker(), temp_dir=Path(directory))
            await manager.start()
            job_id = await manager.submit("user-a", audio)
            try:
                self.assertIsNone(await manager.get("user-b", job_id))
                for _ in range(100):
                    result = await manager.get("user-a", job_id)
                    if result and result["state"] in {"complete", "failed"}:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(result, {
                    "job_id": job_id,
                    "state": "complete",
                    "turns": [{"start_seconds": 0.1, "end_seconds": 0.5, "speaker_id": "speaker_1"}],
                })
                self.assertFalse(audio.exists())
            finally:
                await manager.close()

    async def test_rejects_jobs_when_bounded_capacity_is_full(self) -> None:
        started = asyncio.Event()
        finish = asyncio.Event()

        class Worker:
            async def diarize(self, _audio):
                started.set()
                await finish.wait()
                return []

        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.wav"
            second = Path(directory) / "second.wav"
            first.write_bytes(wav_bytes())
            second.write_bytes(wav_bytes())
            manager = DiarizationJobManager(Worker(), max_jobs=1)
            await manager.start()
            try:
                await manager.submit("user-a", first)
                await asyncio.wait_for(started.wait(), timeout=1)
                with self.assertRaisesRegex(ServiceError, "busy"):
                    await manager.submit("user-b", second)
                finish.set()
            finally:
                finish.set()
                await manager.close()


if __name__ == "__main__":
    unittest.main()
