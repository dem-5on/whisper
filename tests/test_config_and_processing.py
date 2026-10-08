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
from transcriber.transcription import FasterWhisperBackend, OpenAICompatibleBackend, WhisperCppBackend, make_backend


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
