"""Provider-neutral Wicx account identities for verified OAuth principals."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
import re
import sqlite3
import stat
from uuid import uuid4


_PROVIDERS = frozenset({"google", "apple"})
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z")


class AccountStoreError(RuntimeError):
    """The private account database is unavailable or invalid."""


@dataclass(frozen=True)
class ResolvedAccount:
    account_id: str
    created: bool


class AccountStore:
    """Map verified provider subjects to permanent internal account IDs.

    This store does not perform OAuth or validate provider tokens. Callers must
    verify issuer, audience, signature, expiry, nonce, and email claims before
    passing a provider subject here. Email is descriptive only and is never an
    account key or an automatic account-linking mechanism.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prepare_private_file()
        try:
            with closing(self._connect()) as connection:
                connection.executescript(
                    """
                    PRAGMA foreign_keys = ON;
                    CREATE TABLE IF NOT EXISTS accounts (
                        account_id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS account_identities (
                        provider TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        account_id TEXT NOT NULL,
                        email TEXT,
                        email_verified INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        PRIMARY KEY (provider, subject),
                        FOREIGN KEY (account_id) REFERENCES accounts(account_id)
                            ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS account_identities_by_account
                        ON account_identities (account_id);
                    """
                )
        except sqlite3.Error as exc:
            raise AccountStoreError("Could not initialize account database") from exc

    def resolve_verified_identity(
        self, *, provider: str, subject: str, email: str | None = None,
        email_verified: bool = False, now: datetime | None = None,
    ) -> ResolvedAccount:
        """Return/create an account for an already-verified Google/Apple subject."""
        if not isinstance(provider, str) or provider not in _PROVIDERS:
            raise ValueError("identity provider must be google or apple")
        if (not isinstance(subject, str) or not subject or len(subject) > 255 or
                subject.strip() != subject or any(ord(char) < 0x20 for char in subject)):
            raise ValueError("provider subject is invalid")
        if not isinstance(email_verified, bool):
            raise ValueError("email verification state is invalid")
        stored_email: str | None = None
        if email_verified:
            if not isinstance(email, str) or len(email) > 254 or not _EMAIL.fullmatch(email):
                raise ValueError("verified provider email is invalid")
            stored_email = email
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("account event time must include a timezone")
        timestamp = current.astimezone(UTC).isoformat(timespec="seconds")
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT account_id FROM account_identities WHERE provider = ? AND subject = ?",
                    (provider, subject),
                ).fetchone()
                if existing:
                    account_id = existing[0]
                    if stored_email is None:
                        connection.execute(
                            "UPDATE account_identities SET last_seen_at = ? WHERE provider = ? AND subject = ?",
                            (timestamp, provider, subject),
                        )
                    else:
                        connection.execute(
                            """UPDATE account_identities
                               SET email = ?, email_verified = 1, last_seen_at = ?
                               WHERE provider = ? AND subject = ?""",
                            (stored_email, timestamp, provider, subject),
                        )
                    connection.commit()
                    return ResolvedAccount(account_id=account_id, created=False)

                account_id = str(uuid4())
                connection.execute(
                    "INSERT INTO accounts (account_id, created_at) VALUES (?, ?)",
                    (account_id, timestamp),
                )
                connection.execute(
                    """INSERT INTO account_identities
                       (provider, subject, account_id, email, email_verified, created_at, last_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (provider, subject, account_id, stored_email, int(stored_email is not None),
                     timestamp, timestamp),
                )
                connection.commit()
                return ResolvedAccount(account_id=account_id, created=True)
        except sqlite3.Error as exc:
            raise AccountStoreError("Could not resolve provider identity") from exc

    def _prepare_private_file(self) -> None:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                mode = stat.S_IMODE(self.path.stat().st_mode)
            except OSError as exc:
                raise AccountStoreError("Could not inspect account database permissions") from exc
            if mode & 0o077:
                raise AccountStoreError("Account database must be owner-only; set permissions to 0600")
        except OSError as exc:
            raise AccountStoreError("Could not create account database") from exc
        else:
            os.close(fd)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
