from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from transcriber.config import (
    ConfigurationError,
    ProcessingConfig,
    TranscriptionConfig,
    _parse,
    default_keys_path,
    load_config,
    load_keys_env,
    resolve_config_path,
    save_config,
    write_key,
)
from transcriber.processing import process_transcript
from transcriber.transcription import (
    FasterWhisperBackend, OpenAICompatibleBackend, WhisperCppBackend, make_backend,
    transcribe_long_audio, transcribe_long_audio_with_segments,
)


class ConfigAndProcessingTests(unittest.TestCase):
    def test_defaults_are_safe_and_local(self) -> None:
        config = _parse({})
        self.assertEqual(config.audio.sample_rate, 16000)
        self.assertEqual(config.audio.channels, 1)
        self.assertEqual(config.audio.format, "wav")
        self.assertEqual(config.transcription.backend, "local")
        self.assertFalse(config.processing.cleanup)
        self.assertFalse(config.delivery.submit_after_insert)

    def test_non_standard_audio_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            _parse({"audio": {"sample_rate": 44100}})

    def test_verbatim_mode_preserves_technical_text(self) -> None:
        text = "kubectl  get pods -n monitoring\n"
        self.assertEqual(process_transcript(text, ProcessingConfig(cleanup=False)), text)

    def test_cleanup_is_explicit(self) -> None:
        self.assertEqual(process_transcript(" a   b ", ProcessingConfig(cleanup=True)), "a b")

    def test_transcription_backend_is_configuration_driven(self) -> None:
        self.assertIsInstance(make_backend(TranscriptionConfig(backend="local", local_engine="faster-whisper")), FasterWhisperBackend)
        self.assertIsInstance(make_backend(TranscriptionConfig(backend="local", local_engine="whisper.cpp")), WhisperCppBackend)
        self.assertIsInstance(make_backend(TranscriptionConfig(backend="groq")), OpenAICompatibleBackend)
        self.assertIsInstance(make_backend(TranscriptionConfig(backend="openrouter")), OpenAICompatibleBackend)

    def test_long_audio_uses_bounded_sample_chunks_when_backend_supports_them(self) -> None:
        import wave

        class ChunkBackend:
            def __init__(self) -> None:
                self.chunk_lengths: list[int] = []

            def transcribe_samples(self, samples) -> str:
                self.chunk_lengths.append(len(samples))
                return f"chunk {len(self.chunk_lengths)}"

            def transcribe(self, _audio: Path) -> str:
                raise AssertionError("long recording should use the bounded sample path")

        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "long.wav"
            with wave.open(str(audio), "wb") as target:
                target.setnchannels(1)
                target.setsampwidth(2)
                target.setframerate(16_000)
                target.writeframes(b"\0\0" * 40_000)
            backend = ChunkBackend()
            result = transcribe_long_audio(backend, audio, chunk_seconds=1)

        self.assertEqual(backend.chunk_lengths, [16_000, 16_000, 8_000])
        self.assertEqual(result, "chunk 1 chunk 2 chunk 3")

    def test_long_audio_offsets_timestamped_segments_across_chunks(self) -> None:
        import wave

        class TimestampBackend:
            def __init__(self) -> None:
                self.offsets: list[float] = []

            def transcribe_samples_with_timestamps(self, samples, *, offset_seconds: float):
                self.offsets.append(offset_seconds)
                return [{
                    "start_seconds": offset_seconds + 0.1,
                    "end_seconds": offset_seconds + 0.5,
                    "text": f"chunk {len(self.offsets)}",
                }]

        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "long.wav"
            with wave.open(str(audio), "wb") as target:
                target.setnchannels(1)
                target.setsampwidth(2)
                target.setframerate(16_000)
                target.writeframes(b"\0\0" * 40_000)
            backend = TimestampBackend()
            transcript, segments = transcribe_long_audio_with_segments(backend, audio, chunk_seconds=1)

        self.assertEqual(backend.offsets, [0.0, 1.0, 2.0])
        self.assertEqual(transcript, "chunk 1 chunk 2 chunk 3")
        self.assertEqual([segment["start_seconds"] for segment in segments], [0.1, 1.1, 2.1])
        self.assertEqual([segment["speaker_id"] for segment in segments], [None, None, None])


class ConfigPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.yaml"
        self.keys = Path(self.tmp.name) / "keys.env"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_save_and_reload_roundtrip(self) -> None:
        config = _parse({"transcription": {"backend": "groq", "model": "whisper-large-v3-turbo"}})
        save_config(config, self.path)
        reloaded = load_config(self.path)
        self.assertEqual(reloaded.transcription.backend, "groq")
        self.assertEqual(reloaded.transcription.model, "whisper-large-v3-turbo")

    def test_resolve_config_path_defaults(self) -> None:
        self.assertEqual(resolve_config_path(self.path), self.path)
        config_folder = "Whisper" if os.name == "nt" else "transcriber"
        self.assertEqual(resolve_config_path().name, "config.yaml")
        self.assertEqual(resolve_config_path().parent.name, config_folder)

    def test_write_key_stores_0600_and_removes(self) -> None:
        write_key(self.keys, "OPENROUTER_API_KEY", "sk-or-secret")
        text = self.keys.read_text()
        self.assertIn("OPENROUTER_API_KEY=sk-or-secret", text)
        if os.name != "nt":
            self.assertEqual(oct(self.keys.stat().st_mode & 0o777), "0o600")
        write_key(self.keys, "GROQ_API_KEY", "gsk-secret")
        write_key(self.keys, "OPENROUTER_API_KEY", None)
        text = self.keys.read_text()
        self.assertNotIn("OPENROUTER_API_KEY", text)
        self.assertIn("GROQ_API_KEY=gsk-secret", text)

    def test_load_keys_env_applies_without_overriding(self) -> None:
        self.keys.write_text("# comment\n\nOPENROUTER_API_KEY=file-key\nGROQ_API_KEY=file-groq\n")
        with patch.dict(os.environ, {"GROQ_API_KEY": "real-env"}, clear=False):
            os.environ.pop("OPENROUTER_API_KEY", None)
            applied = load_keys_env(self.keys)
            self.assertEqual(applied, {"OPENROUTER_API_KEY": "file-key"})
            self.assertEqual(os.environ["GROQ_API_KEY"], "real-env")
            self.assertEqual(os.environ["OPENROUTER_API_KEY"], "file-key")

    def test_load_keys_env_missing_file_is_empty(self) -> None:
        self.assertEqual(load_keys_env(self.keys), {})

    def test_default_keys_path(self) -> None:
        config_folder = "Whisper" if os.name == "nt" else "transcriber"
        self.assertEqual(default_keys_path().name, "keys.env")
        self.assertEqual(default_keys_path().parent.name, config_folder)
