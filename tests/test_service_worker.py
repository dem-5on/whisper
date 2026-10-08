from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from whisper_service.hardware import HardwareSelection
from whisper_service.sessions import ModelProfile
from whisper_service.worker import FasterWhisperWorker, WorkerStartupError, prepare_worker


class FakeModel:
    def __init__(self) -> None:
        self.options: list[dict[str, object]] = []

    def transcribe(self, samples, **options):
        self.options.append(options)
        call = len(self.options)
        texts = {
            1: "",  # startup warm-up
            2: "one two three",
            3: "one two three four",
            4: "three four five",
            5: "final transcript",
        }
        return iter([SimpleNamespace(text=texts.get(call, ""))]), SimpleNamespace(language="en")


class FasterWhisperWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_loads_model_once_streams_rolling_hypotheses_and_finalizes(self) -> None:
        model = FakeModel()
        calls = []

        def factory(name, **kwargs):
            calls.append((name, kwargs))
            return model

        hardware = HardwareSelection("cpu", "cpu", "int8", 0, (), "CPU explicitly selected")
        worker = FasterWhisperWorker(
            hardware, partial_interval_seconds=0.5, rolling_window_seconds=1,
            cpu_threads=3, model_factory=factory,
        )
        profile = ModelProfile("default", "base")
        await worker.prepare(profile)
        session = await worker.open_session(profile, "en")
        chunk = b"\x01\x00" * 8000  # 0.5 s of 16 kHz mono PCM16
        self.assertEqual(await session.submit_audio(chunk), "one two three")
        self.assertEqual(await session.submit_audio(chunk), "one two three four")
        # The rolling window has advanced. Preserve its overlapping prefix and
        # append only the genuinely new words to the full current hypothesis.
        self.assertEqual(await session.submit_audio(chunk), "one two three four five")
        self.assertEqual(await session.finish(), "final transcript")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "base")
        self.assertEqual(calls[0][1], {
            "device": "cpu", "compute_type": "int8", "cpu_threads": 3,
        })
        self.assertEqual([options["beam_size"] for options in model.options], [1, 1, 1, 1, 5])
        self.assertEqual(session._all_audio, bytearray())
        self.assertEqual(session._rolling_audio, bytearray())

    async def test_cancel_clears_all_audio_and_closes_session(self) -> None:
        model = FakeModel()
        hardware = HardwareSelection("cpu", "cpu", "int8", 0, (), "CPU explicitly selected")
        worker = FasterWhisperWorker(hardware, model_factory=lambda *_args, **_kwargs: model)
        profile = ModelProfile("default", "base")
        session = await worker.open_session(profile, None)
        await session.submit_audio(b"\x01\x00" * 100)
        await session.cancel()
        self.assertEqual(session._all_audio, bytearray())
        self.assertEqual(session._rolling_audio, bytearray())
        self.assertIsNone(await session.submit_audio(b"\x01\x00" * 100))

    async def test_profile_rejects_wrong_device(self) -> None:
        hardware = HardwareSelection("cpu", "cpu", "int8", 0, (), "CPU explicitly selected")
        worker = FasterWhisperWorker(hardware, model_factory=lambda *_args, **_kwargs: FakeModel())
        with self.assertRaisesRegex(WorkerStartupError, "not compatible"):
            await worker.open_session(ModelProfile("cuda-only", "large", ("cuda",)), None)

    async def test_auto_mode_falls_back_to_cpu_when_cuda_model_wont_load(self) -> None:
        cuda = HardwareSelection("auto", "cuda", "float16", 1, ("float16",), "CUDA available")
        cpu = HardwareSelection("cpu", "cpu", "int8", 0, (), "CPU explicitly selected")
        failed_gpu_worker = AsyncMock()
        failed_gpu_worker.prepare.side_effect = WorkerStartupError("GPU model failed")
        cpu_worker = AsyncMock()
        profile = ModelProfile("default", "base")
        with patch("whisper_service.worker.detect_hardware", side_effect=[cuda, cpu]), patch(
            "whisper_service.worker.FasterWhisperWorker", side_effect=[failed_gpu_worker, cpu_worker]
        ):
            selection, worker = await prepare_worker("auto", profile)
        self.assertEqual(selection.device, "cpu")
        self.assertIn("CUDA model initialization failed", selection.reason)
        self.assertIs(worker, cpu_worker)
        failed_gpu_worker.prepare.assert_awaited_once_with(profile)
        cpu_worker.prepare.assert_awaited_once_with(profile)


if __name__ == "__main__":
    unittest.main()
