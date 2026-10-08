from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
import wave
from importlib.util import find_spec
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

from transcriber.config import TranscriptionConfig
from transcriber.transcription import FasterWhisperBackend, OpenAICompatibleBackend, TranscriptionError, _multipart, make_backend, read_standard_wav


class TranscriptionTests(unittest.TestCase):
    def test_openai_batch_backend_uses_official_transcription_endpoint(self) -> None:
        backend = make_backend(TranscriptionConfig(backend="openai", model="gpt-transcribe"))
        self.assertIsInstance(backend, OpenAICompatibleBackend)
        self.assertEqual(backend.endpoint, "https://api.openai.com/v1/audio/transcriptions")
        self.assertEqual(backend.api_key_env, "OPENAI_API_KEY")
        self.assertEqual(backend.model, "gpt-transcribe")

    def test_missing_key_does_not_require_or_expose_a_config_key(self) -> None:
        previous = os.environ.pop("TRANSCRIBER_TEST_KEY", None)
        try:
            with self.assertRaisesRegex(TranscriptionError, "TRANSCRIBER_TEST_KEY"):
                OpenAICompatibleBackend("https://example.invalid", "TRANSCRIBER_TEST_KEY", "model").transcribe(Path("unused.wav"))
        finally:
            if previous is not None:
                os.environ["TRANSCRIBER_TEST_KEY"] = previous

    def test_multipart_request_contains_model_and_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "voice.wav"
            audio.write_bytes(b"audio-data")
            request = _multipart("boundary", audio, "whisper-large-v3")
        self.assertIn(b'name="model"', request)
        self.assertIn(b"whisper-large-v3", request)
        self.assertIn(b"audio-data", request)

    @unittest.skipUnless(find_spec("numpy"), "numpy is installed with the local backend extra")
    def test_standard_wav_is_decoded_without_pyav(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "voice.wav"
            with wave.open(str(audio), "wb") as source:
                source.setnchannels(1)
                source.setsampwidth(2)
                source.setframerate(16_000)
                source.writeframes(b"\x00\x80\x00\x00\xff\x7f")
            samples = read_standard_wav(audio)
        self.assertEqual(samples.tolist(), [-1.0, 0.0, 32767 / 32768])

    @unittest.skipUnless(find_spec("numpy"), "numpy is installed with the local backend extra")
    def test_local_backend_passes_normalized_samples_not_a_path(self) -> None:
        fake_model = MagicMock()
        fake_model.transcribe.return_value = ([MagicMock(text="hello")], None)
        module = ModuleType("faster_whisper")
        module.WhisperModel = MagicMock(return_value=fake_model)  # type: ignore[attr-defined]
        with patch.dict(sys.modules, {"faster_whisper": module}), patch("transcriber.transcription.read_standard_wav", return_value=MagicMock()):
            self.assertEqual(FasterWhisperBackend("base").transcribe(Path("audio.wav")), "hello")
        fake_model.transcribe.assert_called_once_with(unittest.mock.ANY)
        module.WhisperModel.assert_called_once_with("base", device="cpu", compute_type="int8")
