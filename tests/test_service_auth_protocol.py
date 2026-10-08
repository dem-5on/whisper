from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from whisper_service.auth import AuthenticationConfigError, TokenAuthenticator, add_user_token, revoke_user_tokens
from whisper_service.protocol import parse_control, parse_session_start
from whisper_service.sessions import ServiceError


class TokenAuthenticatorTests(unittest.TestCase):
    def test_issue_token_persists_only_digest_with_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tokens.json"
            token = add_user_token(path, "friend-1")
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn(token, raw)
            self.assertIn(hashlib.sha256(token.encode()).hexdigest(), raw)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            auth = TokenAuthenticator.from_file(path)
            self.assertEqual(auth.authenticate(f"Bearer {token}"), "friend-1")
            self.assertEqual(auth.authenticate(f"bearer {token}"), "friend-1")
            self.assertIsNone(auth.authenticate(token))
            self.assertIsNone(auth.authenticate(f"Bearer {token} "))

    def test_revoke_removes_all_user_tokens_but_preserves_other_users(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tokens.json"
            first = add_user_token(path, "friend-1")
            other = add_user_token(path, "friend-2")
            self.assertEqual(revoke_user_tokens(path, "friend-1"), 1)
            auth = TokenAuthenticator.from_file(path)
            self.assertIsNone(auth.authenticate(f"Bearer {first}"))
            self.assertEqual(auth.authenticate(f"Bearer {other}"), "friend-2")

    @unittest.skipIf(os.name == "nt", "Unix permission bits are not available on Windows")
    def test_refuses_group_or_world_readable_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tokens.json"
            path.write_text(json.dumps({"tokens": {}}), encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaisesRegex(AuthenticationConfigError, "set permissions to 0600"):
                TokenAuthenticator.from_file(path)


class LiveProtocolValidationTests(unittest.TestCase):
    def test_parses_fixed_audio_format_and_auto_language(self) -> None:
        profile, language = parse_session_start(json.dumps({
            "type": "session.start",
            "audio": {"encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1},
            "profile": "default",
            "language": "auto",
        }))
        self.assertEqual((profile, language), ("default", None))

    def test_rejects_model_names_as_profile_and_unsupported_audio(self) -> None:
        for payload in (
            {"type": "session.start", "profile": "", "audio": {"encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1}},
            {"type": "session.start", "profile": "default", "audio": {"encoding": "wav", "sample_rate_hz": 44100, "channels": 2}},
        ):
            with self.assertRaises(ServiceError):
                parse_session_start(json.dumps(payload))

    def test_only_finish_and_cancel_are_valid_controls(self) -> None:
        self.assertEqual(parse_control('{"type":"session.finish"}'), "session.finish")
        self.assertEqual(parse_control('{"type":"session.cancel"}'), "session.cancel")
        with self.assertRaises(ServiceError):
            parse_control('{"type":"session.start"}')


if __name__ == "__main__":
    unittest.main()
