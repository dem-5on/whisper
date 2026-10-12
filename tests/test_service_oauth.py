from __future__ import annotations

from datetime import UTC, datetime, timedelta
import base64
import hashlib
from pathlib import Path
import stat
import tempfile
import unittest

from whisper_service.oauth import OAuthTransactionError, OAuthTransactionStore


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


class OAuthTransactionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "oauth.sqlite3"
        self.store = OAuthTransactionStore(self.path)
        self.now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
        self.verifier = "desktop-verifier-0123456789-abcdefghijklmnopqrstuvwxyz"

    def _start(self, provider: str = "google"):
        return self.store.begin(
            provider=provider, pkce_challenge=_challenge(self.verifier), now=self.now,
        )

    def test_callback_nonce_and_pkce_protect_one_time_handoff(self) -> None:
        started = self._start()
        pending = self.store.pending_callback(provider="google", state=started.state, now=self.now)
        self.assertEqual(pending.nonce, started.nonce)
        self.assertEqual(pending.pkce_challenge, _challenge(self.verifier))
        code = self.store.complete_callback(
            provider="google", state=started.state, verified_nonce=pending.nonce,
            account_id="account-1", now=self.now,
        )
        self.assertEqual(
            self.store.redeem_handoff(code=code, pkce_verifier=self.verifier, now=self.now),
            "account-1",
        )
        with self.assertRaises(OAuthTransactionError):
            self.store.redeem_handoff(code=code, pkce_verifier=self.verifier, now=self.now)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_wrong_provider_nonce_or_state_cannot_complete_transaction(self) -> None:
        started = self._start()
        with self.assertRaises(OAuthTransactionError):
            self.store.pending_callback(provider="apple", state=started.state, now=self.now)
        with self.assertRaises(OAuthTransactionError):
            self.store.complete_callback(
                provider="google", state=started.state, verified_nonce="wrong",
                account_id="account-1", now=self.now,
            )
        # Failed attempts do not consume a legitimate transaction.
        pending = self.store.pending_callback(provider="google", state=started.state, now=self.now)
        code = self.store.complete_callback(
            provider="google", state=started.state, verified_nonce=pending.nonce,
            account_id="account-1", now=self.now,
        )
        with self.assertRaises(OAuthTransactionError):
            self.store.complete_callback(
                provider="google", state=started.state, verified_nonce=pending.nonce,
                account_id="account-1", now=self.now,
            )
        self.assertEqual(self.store.redeem_handoff(code=code, pkce_verifier=self.verifier), "account-1")

    def test_transactions_and_handoffs_expire(self) -> None:
        started = self._start()
        with self.assertRaises(OAuthTransactionError):
            self.store.pending_callback(
                provider="google", state=started.state, now=self.now + timedelta(minutes=10),
            )
        second = self._start("apple")
        code = self.store.complete_callback(
            provider="apple", state=second.state, verified_nonce=second.nonce,
            account_id="account-2", now=self.now,
        )
        with self.assertRaises(OAuthTransactionError):
            self.store.redeem_handoff(
                code=code, pkce_verifier=self.verifier, now=self.now + timedelta(minutes=2),
            )

    def test_wrong_pkce_verifier_does_not_consume_handoff(self) -> None:
        started = self._start()
        code = self.store.complete_callback(
            provider="google", state=started.state, verified_nonce=started.nonce,
            account_id="account-1", now=self.now,
        )
        with self.assertRaises(OAuthTransactionError):
            self.store.redeem_handoff(code=code, pkce_verifier="x" * 48, now=self.now)
        self.assertEqual(self.store.redeem_handoff(code=code, pkce_verifier=self.verifier), "account-1")

    def test_rejects_invalid_provider_pkce_and_naive_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "provider"):
            self.store.begin(provider="github", pkce_challenge=_challenge(self.verifier))
        with self.assertRaisesRegex(ValueError, "PKCE"):
            self.store.begin(provider="google", pkce_challenge="short")
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.store.begin(
                provider="google", pkce_challenge=_challenge(self.verifier),
                now=datetime(2026, 10, 12),
            )


if __name__ == "__main__":
    unittest.main()
