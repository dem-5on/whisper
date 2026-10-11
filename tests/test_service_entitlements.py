from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from whisper_service.auth import TokenAuthenticator
from whisper_service.entitlements import (
    EntitlementConfigError,
    EntitlementStore,
    grant_feature,
    list_features,
    revoke_feature,
)
from whisper_service.server import LIVE_PATH, make_process_request


class EntitlementStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "entitlements.json"
        self.store = EntitlementStore(self.path)

    def test_missing_grants_deny_access_by_default(self) -> None:
        self.assertFalse(self.store.has_feature("user-a", "diarization"))

    def test_grants_are_owner_only_and_revoke_immediately(self) -> None:
        grant_feature(self.path, "user-a", "diarization")
        self.assertTrue(self.store.has_feature("user-a", "diarization"))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(revoke_feature(self.path, "user-a", "diarization"))
        self.assertFalse(self.store.has_feature("user-a", "diarization"))
        self.assertFalse(revoke_feature(self.path, "user-a", "diarization"))

    def test_expiring_grants_use_timezone_aware_utc_comparison(self) -> None:
        grant_feature(self.path, "user-a", "diarization", expires_at="2030-01-01T00:00:00Z")
        self.assertTrue(self.store.has_feature(
            "user-a", "diarization", now=datetime(2029, 12, 31, 23, 59, tzinfo=UTC)
        ))
        self.assertFalse(self.store.has_feature(
            "user-a", "diarization", now=datetime(2030, 1, 1, tzinfo=UTC)
        ))

    def test_malformed_or_readable_by_others_store_fails_closed(self) -> None:
        grant_feature(self.path, "user-a", "diarization")
        self.path.chmod(0o644)
        with self.assertRaisesRegex(EntitlementConfigError, "owner-only"):
            self.store.has_feature("user-a", "diarization")
        self.path.chmod(0o600)
        self.path.write_text("not json", encoding="utf-8")
        with self.assertRaises(EntitlementConfigError):
            list_features(self.path)

    def test_rejects_naive_expiry_and_invalid_feature_names(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone"):
            grant_feature(self.path, "user-a", "diarization", expires_at="2030-01-01T00:00:00")
        with self.assertRaisesRegex(ValueError, "lowercase identifier"):
            grant_feature(self.path, "user-a", "Diarization")

    def test_live_transcription_handshake_requires_paid_feature(self) -> None:
        token = "user-a-token"
        auth = TokenAuthenticator({hashlib.sha256(token.encode()).hexdigest(): "user-a"})
        process_request = make_process_request(auth, self.store)

        class Connection:
            def respond(self, status, body):
                return status, body

        request = SimpleNamespace(
            path=LIVE_PATH, method="GET", headers={"Authorization": f"Bearer {token}"}
        )
        denied = asyncio.run(process_request(Connection(), request))
        self.assertEqual(denied[0], 402)

        grant_feature(self.path, "user-a", "cloud_transcription")
        self.assertIsNone(asyncio.run(process_request(Connection(), request)))
        self.path.write_text("invalid", encoding="utf-8")
        unavailable = asyncio.run(process_request(Connection(), request))
        self.assertEqual(unavailable[0], 503)


if __name__ == "__main__":
    unittest.main()
