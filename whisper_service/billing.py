"""Durable, provider-neutral subscription state for verified billing events.

This module does not call payment providers or verify webhook signatures. Its
caller must authenticate an event before applying it here. Provider-specific
checkout and webhook adapters can then share one idempotent persistence layer.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
import os
from pathlib import Path
import re
import sqlite3
import stat


_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:@+-]{1,200}\Z")
_STATUSES = frozenset({"active", "non_renewing", "attention", "paused", "cancelled", "completed", "expired"})


class BillingStoreError(RuntimeError):
    """The billing database is unavailable or contains invalid state."""


class BillingStore:
    """Store account-linked subscriptions and deduplicate verified events."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prepare_private_file()
        try:
            with closing(self._connect()) as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS subscriptions (
                        provider TEXT NOT NULL,
                        provider_subscription_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        plan_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        current_period_end TEXT,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (provider, provider_subscription_id)
                    );
                    CREATE INDEX IF NOT EXISTS subscriptions_by_user
                        ON subscriptions (user_id, status);
                    CREATE TABLE IF NOT EXISTS webhook_events (
                        provider TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        received_at TEXT NOT NULL,
                        PRIMARY KEY (provider, event_id)
                    );
                    CREATE TABLE IF NOT EXISTS checkout_attempts (
                        reference TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        plan_id TEXT NOT NULL,
                        amount_minor INTEGER NOT NULL,
                        currency TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    """
                )
        except sqlite3.Error as exc:
            raise BillingStoreError("Could not initialize billing database") from exc

    def apply_verified_event(
        self,
        *,
        provider: str,
        event_id: str,
        subscription_id: str,
        user_id: str,
        plan_id: str,
        status: str,
        current_period_end: str | None,
        now: datetime | None = None,
    ) -> bool:
        """Atomically apply an already-verified event; return false on replay."""
        for name, value in (
            ("provider", provider), ("event ID", event_id),
            ("subscription ID", subscription_id), ("user ID", user_id),
            ("plan ID", plan_id),
        ):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"{name} is invalid")
        if not isinstance(status, str) or status not in _STATUSES:
            raise ValueError("subscription status is invalid")
        expiry = _normalize_timestamp(current_period_end)
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("event time must include a timezone")
        received_at = current.astimezone(UTC).isoformat(timespec="seconds")
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO webhook_events (provider, event_id, received_at) VALUES (?, ?, ?)",
                    (provider, event_id, received_at),
                )
                if cursor.rowcount == 0:
                    connection.rollback()
                    return False
                connection.execute(
                    """INSERT INTO subscriptions
                       (provider, provider_subscription_id, user_id, plan_id, status,
                        current_period_end, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(provider, provider_subscription_id) DO UPDATE SET
                         user_id = excluded.user_id,
                         plan_id = excluded.plan_id,
                         status = excluded.status,
                         current_period_end = excluded.current_period_end,
                         updated_at = excluded.updated_at""",
                    (provider, subscription_id, user_id, plan_id, status, expiry, received_at),
                )
                connection.commit()
                return True
        except sqlite3.Error as exc:
            raise BillingStoreError("Could not persist billing event") from exc

    def get_subscription(self, provider: str, subscription_id: str) -> dict[str, str | None] | None:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    """SELECT provider, provider_subscription_id, user_id, plan_id, status,
                              current_period_end, updated_at
                       FROM subscriptions WHERE provider = ? AND provider_subscription_id = ?""",
                    (provider, subscription_id),
                ).fetchone()
        except sqlite3.Error as exc:
            raise BillingStoreError("Could not read billing state") from exc
        if row is None:
            return None
        keys = ("provider", "subscription_id", "user_id", "plan_id", "status", "current_period_end", "updated_at")
        return dict(zip(keys, row, strict=True))

    def record_checkout(
        self, *, reference: str, user_id: str, plan_id: str,
        amount_minor: int, currency: str, now: datetime | None = None,
    ) -> None:
        """Persist Paystack's checkout reference before returning it to a client."""
        for name, value in (("reference", reference), ("user ID", user_id), ("plan ID", plan_id)):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"{name} is invalid")
        if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
            raise ValueError("checkout amount must be a positive integer")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("checkout currency must be a three-letter uppercase code")
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("checkout time must include a timezone")
        created_at = current.astimezone(UTC).isoformat(timespec="seconds")
        try:
            with closing(self._connect()) as connection:
                connection.execute(
                    """INSERT INTO checkout_attempts
                       (reference, user_id, plan_id, amount_minor, currency, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (reference, user_id, plan_id, amount_minor, currency, created_at),
                )
        except sqlite3.IntegrityError as exc:
            raise BillingStoreError("Checkout reference already exists") from exc
        except sqlite3.Error as exc:
            raise BillingStoreError("Could not persist checkout reference") from exc

    def get_checkout(self, reference: str) -> dict[str, str | int] | None:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    """SELECT reference, user_id, plan_id, amount_minor, currency, created_at
                       FROM checkout_attempts WHERE reference = ?""",
                    (reference,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise BillingStoreError("Could not read checkout reference") from exc
        if row is None:
            return None
        keys = ("reference", "user_id", "plan_id", "amount_minor", "currency", "created_at")
        return dict(zip(keys, row, strict=True))

    def _prepare_private_file(self) -> None:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                mode = stat.S_IMODE(self.path.stat().st_mode)
            except OSError as exc:
                raise BillingStoreError("Could not inspect billing database permissions") from exc
            if mode & 0o077:
                raise BillingStoreError("Billing database must be owner-only; set permissions to 0600")
        except OSError as exc:
            raise BillingStoreError("Could not create billing database") from exc
        else:
            os.close(fd)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = None
        return connection


def _normalize_timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("subscription period end must be an ISO-8601 timestamp")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("subscription period end must be an ISO-8601 timestamp") from exc
    if timestamp.tzinfo is None:
        raise ValueError("subscription period end must include a timezone")
    return timestamp.astimezone(UTC).isoformat(timespec="seconds")
