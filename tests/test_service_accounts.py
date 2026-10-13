from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import stat
import sqlite3
import tempfile
import unittest

from whisper_service.accounts import AccountStore, AccountStoreError


class AccountStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "accounts.sqlite3"
        self.store = AccountStore(self.path)

    def test_verified_provider_subject_resolves_to_stable_internal_account(self) -> None:
        now = datetime(2026, 10, 12, tzinfo=UTC)
        first = self.store.resolve_verified_identity(
            provider="google", subject="google-subject-123",
            email="person@example.com", email_verified=True, now=now,
        )
        second = self.store.resolve_verified_identity(
            provider="google", subject="google-subject-123",
            email="changed@example.com", email_verified=True,
            now=datetime(2026, 10, 13, tzinfo=UTC),
        )
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.account_id, second.account_id)
        self.assertEqual(len(first.account_id), 36)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_email_is_not_an_identity_or_automatic_account_link(self) -> None:
        google = self.store.resolve_verified_identity(
            provider="google", subject="google-subject",
            email="same@example.com", email_verified=True,
        )
        apple = self.store.resolve_verified_identity(
            provider="apple", subject="apple-subject",
            email="same@example.com", email_verified=True,
        )
        self.assertNotEqual(google.account_id, apple.account_id)

    def test_unverified_email_is_never_used_as_account_identity_or_saved(self) -> None:
        first = self.store.resolve_verified_identity(
            provider="apple", subject="apple-private-relay-subject",
            email="untrusted@example.com", email_verified=False,
        )
        second = self.store.resolve_verified_identity(
            provider="apple", subject="apple-private-relay-subject",
            email=None, email_verified=False,
        )
        self.assertEqual(first.account_id, second.account_id)
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT email, email_verified FROM account_identities WHERE provider = 'apple'",
            ).fetchone()
        self.assertEqual(row, (None, 0))

    def test_provider_subjects_are_separate_and_supported_providers_are_allowlisted(self) -> None:
        google = self.store.resolve_verified_identity(provider="google", subject="subject")
        different_google_subject = self.store.resolve_verified_identity(
            provider="google", subject="other-subject",
        )
        self.assertNotEqual(google.account_id, different_google_subject.account_id)
        for provider in ("github", []):
            with self.subTest(provider=provider), self.assertRaisesRegex(ValueError, "google or apple"):
                self.store.resolve_verified_identity(provider=provider, subject="subject")
        for subject in ("", " subject", "subject ", "bad\nsubject", "s" * 256):
            with self.subTest(subject=subject), self.assertRaisesRegex(ValueError, "subject"):
                self.store.resolve_verified_identity(provider="google", subject=subject)

    def test_only_verified_valid_email_is_saved(self) -> None:
        with self.assertRaisesRegex(ValueError, "verified provider email"):
            self.store.resolve_verified_identity(
                provider="google", subject="subject", email="not-an-email", email_verified=True,
            )

    def test_world_readable_database_fails_closed(self) -> None:
        self.path.chmod(0o644)
        with self.assertRaises(AccountStoreError):
            AccountStore(self.path)

    def test_account_sessions_are_hashed_expiring_and_account_scoped(self) -> None:
        created = self.store.resolve_verified_identity(provider="google", subject="session-subject")
        now = datetime(2026, 10, 12, tzinfo=UTC)
        token = self.store.issue_session(created.account_id, now=now)
        self.assertEqual(self.store.authenticate_session(token, now=now), created.account_id)
        self.assertIsNone(self.store.authenticate_session(token + "x", now=now))
        self.assertIsNone(self.store.authenticate_session(token, now=now + timedelta(days=30)))
        with sqlite3.connect(self.path) as connection:
            stored = connection.execute("SELECT token_digest FROM account_sessions").fetchone()[0]
        self.assertNotEqual(stored, token)
        self.assertEqual(len(stored), 64)

    def test_cannot_issue_session_for_unknown_account(self) -> None:
        with self.assertRaisesRegex(AccountStoreError, "unknown account"):
            self.store.issue_session("not-an-account")


if __name__ == "__main__":
    unittest.main()
