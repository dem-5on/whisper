"""Provider-neutral Wicx account identities for verified OAuth principals."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import timedelta
import hashlib
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from uuid import uuid4


_PROVIDERS = frozenset({"google", "apple"})
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z")
_SESSION_TTL = timedelta(days=30)


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
                    CREATE TABLE IF NOT EXISTS account_sessions (
                        token_digest TEXT PRIMARY KEY,
                        account_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        FOREIGN KEY (account_id) REFERENCES accounts(account_id) ON DELETE CASCADE
                    );
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

    def issue_session(self, account_id: str, *, now: datetime | None = None) -> str:
        """Issue a 30-day opaque bearer token, storing only its digest."""
        if not isinstance(account_id, str) or not account_id.strip() or len(account_id) > 128:
            raise ValueError("account ID is invalid")
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("session event time must include a timezone")
        current = current.astimezone(UTC)
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM account_sessions WHERE expires_at <= ?", (_stamp(current),))
                exists = connection.execute(
                    "SELECT 1 FROM accounts WHERE account_id = ?", (account_id,),
                ).fetchone()
                if exists is None:
                    connection.rollback()
                    raise AccountStoreError("Cannot issue a session for an unknown account")
                connection.execute(
                    "INSERT INTO account_sessions VALUES (?, ?, ?, ?)",
                    (digest, account_id, _stamp(current), _stamp(current + _SESSION_TTL)),
                )
                connection.commit()
        except AccountStoreError:
            raise
        except sqlite3.Error as exc:
            raise AccountStoreError("Could not issue account session") from exc
        return token

    def authenticate_session(self, token: str | None, *, now: datetime | None = None) -> str | None:
        """Resolve an unexpired session token without retaining its raw value."""
        if not isinstance(token, str) or not token or token.strip() != token or len(token) > 256:
            return None
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("session event time must include a timezone")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT account_id, expires_at FROM account_sessions WHERE token_digest = ?",
                    (digest,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise AccountStoreError("Could not authenticate account session") from exc
        if row is None or datetime.fromisoformat(row[1]) <= current.astimezone(UTC):
            return None
        return row[0]

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


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")
