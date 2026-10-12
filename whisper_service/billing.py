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
from typing import Collection, Mapping


_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:@+-]{1,200}\Z")
_FEATURE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_STATUSES = frozenset({"active", "non_renewing", "attention", "paused", "cancelled", "completed", "expired", "refunded", "disputed"})


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
                        status_before_dispute TEXT,
                        current_period_end TEXT,
                        last_provider_event_at TEXT,
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
                    CREATE TABLE IF NOT EXISTS payment_transactions (
                        provider TEXT NOT NULL,
                        transaction_reference TEXT NOT NULL,
                        provider_subscription_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (provider, transaction_reference),
                        FOREIGN KEY (provider, provider_subscription_id)
                            REFERENCES subscriptions (provider, provider_subscription_id)
                    );
                    """
                )
                subscription_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(subscriptions)")
                }
                if "status_before_dispute" not in subscription_columns:
                    connection.execute(
                        "ALTER TABLE subscriptions ADD COLUMN status_before_dispute TEXT"
                    )
                if "last_provider_event_at" not in subscription_columns:
                    connection.execute(
                        "ALTER TABLE subscriptions ADD COLUMN last_provider_event_at TEXT"
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
        transaction_reference: str | None = None,
        event_time: str | None = None,
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
        if transaction_reference is not None and (
            not isinstance(transaction_reference, str) or
            not _IDENTIFIER.fullmatch(transaction_reference)
        ):
            raise ValueError("transaction reference is invalid")
        expiry = _normalize_timestamp(current_period_end)
        provider_event_time = _normalize_timestamp(event_time)
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
                if transaction_reference is not None:
                    existing_payment = connection.execute(
                        """SELECT provider_subscription_id FROM payment_transactions
                           WHERE provider = ? AND transaction_reference = ?""",
                        (provider, transaction_reference),
                    ).fetchone()
                    if existing_payment and existing_payment[0] != subscription_id:
                        connection.rollback()
                        raise BillingStoreError("Payment reference is linked to another subscription")
                existing_subscription = connection.execute(
                    """SELECT last_provider_event_at FROM subscriptions
                       WHERE provider = ? AND provider_subscription_id = ?""",
                    (provider, subscription_id),
                ).fetchone()
                if transaction_reference is not None:
                    connection.execute(
                        """INSERT OR IGNORE INTO payment_transactions
                           (provider, transaction_reference, provider_subscription_id, created_at)
                           VALUES (?, ?, ?, ?)""",
                        (provider, transaction_reference, subscription_id, received_at),
                    )
                if (existing_subscription and provider_event_time and
                        existing_subscription[0] and provider_event_time < existing_subscription[0]):
                    connection.commit()
                    return True
                connection.execute(
                    """INSERT INTO subscriptions
                       (provider, provider_subscription_id, user_id, plan_id, status,
                        current_period_end, last_provider_event_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(provider, provider_subscription_id) DO UPDATE SET
                         user_id = excluded.user_id,
                         plan_id = excluded.plan_id,
                         status = excluded.status,
                         current_period_end = excluded.current_period_end,
                         last_provider_event_at = coalesce(excluded.last_provider_event_at,
                                                           subscriptions.last_provider_event_at),
                         updated_at = excluded.updated_at""",
                    (provider, subscription_id, user_id, plan_id, status, expiry,
                     provider_event_time, received_at),
                )
                connection.commit()
                return True
        except BillingStoreError:
            raise
        except sqlite3.Error as exc:
            raise BillingStoreError("Could not persist billing event") from exc

    def apply_payment_status_event(
        self, *, provider: str, event_id: str, transaction_reference: str,
        status: str, event_time: str | None = None, now: datetime | None = None,
    ) -> bool:
        """Apply a verified refund/dispute state only to a known payment."""
        for name, value in (("provider", provider), ("event ID", event_id),
                            ("transaction reference", transaction_reference)):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"{name} is invalid")
        if status not in {"refunded", "disputed"}:
            raise ValueError("payment lifecycle status is invalid")
        provider_event_time = _normalize_timestamp(event_time)
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("event time must include a timezone")
        received_at = current.astimezone(UTC).isoformat(timespec="seconds")
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                payment = connection.execute(
                    """SELECT provider_subscription_id FROM payment_transactions
                       WHERE provider = ? AND transaction_reference = ?""",
                    (provider, transaction_reference),
                ).fetchone()
                if payment is None:
                    connection.rollback()
                    return False
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO webhook_events (provider, event_id, received_at) VALUES (?, ?, ?)",
                    (provider, event_id, received_at),
                )
                if cursor.rowcount == 0:
                    connection.rollback()
                    return False
                subscription = connection.execute(
                    """SELECT status, last_provider_event_at FROM subscriptions
                       WHERE provider = ? AND provider_subscription_id = ?""",
                    (provider, payment[0]),
                ).fetchone()
                if subscription is not None:
                    old_status = subscription[0]
                    if status == "disputed" and old_status not in {"disputed", "refunded"}:
                        connection.execute(
                            """UPDATE subscriptions SET status = 'disputed',
                               status_before_dispute = ?,
                               last_provider_event_at = CASE
                                   WHEN ? IS NOT NULL AND (last_provider_event_at IS NULL OR ? > last_provider_event_at)
                                   THEN ? ELSE last_provider_event_at END,
                               updated_at = ?
                               WHERE provider = ? AND provider_subscription_id = ?""",
                            (old_status, provider_event_time, provider_event_time, provider_event_time,
                             received_at, provider, payment[0]),
                        )
                    elif status == "disputed" and old_status == "disputed":
                        if (provider_event_time and subscription[1] and
                                provider_event_time > subscription[1]):
                            connection.execute(
                                """UPDATE subscriptions SET last_provider_event_at = ?, updated_at = ?
                                   WHERE provider = ? AND provider_subscription_id = ?""",
                                (provider_event_time, received_at, provider, payment[0]),
                            )
                    elif status == "refunded":
                        connection.execute(
                            """UPDATE subscriptions SET status = 'refunded',
                               status_before_dispute = NULL,
                               last_provider_event_at = CASE
                                   WHEN ? IS NOT NULL AND (last_provider_event_at IS NULL OR ? > last_provider_event_at)
                                   THEN ? ELSE last_provider_event_at END,
                               updated_at = ?
                               WHERE provider = ? AND provider_subscription_id = ?""",
                            (provider_event_time, provider_event_time, provider_event_time,
                             received_at, provider, payment[0]),
                        )
                connection.commit()
                return True
        except sqlite3.Error as exc:
            raise BillingStoreError("Could not persist payment lifecycle event") from exc

    def apply_dispute_resolution_event(
        self, *, provider: str, event_id: str, transaction_reference: str,
        resolution: str, event_time: str | None = None, now: datetime | None = None,
    ) -> bool:
        """Apply a signed, resolved Paystack outcome without guessing its result."""
        for name, value in (("provider", provider), ("event ID", event_id),
                            ("transaction reference", transaction_reference)):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"{name} is invalid")
        if resolution not in {"merchant-accepted", "declined"}:
            raise ValueError("dispute resolution is invalid")
        provider_event_time = _normalize_timestamp(event_time)
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("event time must include a timezone")
        received_at = current.astimezone(UTC).isoformat(timespec="seconds")
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                payment = connection.execute(
                    """SELECT provider_subscription_id FROM payment_transactions
                       WHERE provider = ? AND transaction_reference = ?""",
                    (provider, transaction_reference),
                ).fetchone()
                if payment is None:
                    connection.rollback()
                    return False
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO webhook_events (provider, event_id, received_at) VALUES (?, ?, ?)",
                    (provider, event_id, received_at),
                )
                if cursor.rowcount == 0:
                    connection.rollback()
                    return False
                if resolution == "merchant-accepted":
                    connection.execute(
                        """UPDATE subscriptions SET status = 'refunded',
                           status_before_dispute = NULL,
                           last_provider_event_at = CASE
                               WHEN ? IS NOT NULL AND (last_provider_event_at IS NULL OR ? > last_provider_event_at)
                               THEN ? ELSE last_provider_event_at END,
                           updated_at = ?
                           WHERE provider = ? AND provider_subscription_id = ?""",
                        (provider_event_time, provider_event_time, provider_event_time,
                         received_at, provider, payment[0]),
                    )
                else:
                    subscription = connection.execute(
                        """SELECT status, status_before_dispute, current_period_end,
                                  last_provider_event_at
                           FROM subscriptions WHERE provider = ? AND provider_subscription_id = ?""",
                        (provider, payment[0]),
                    ).fetchone()
                    stale_resolution = bool(
                        subscription and provider_event_time and subscription[3] and
                        provider_event_time < subscription[3]
                    )
                    if subscription and subscription[0] == "disputed" and not stale_resolution:
                        restored_status = subscription[1]
                        period_end = _normalize_timestamp(subscription[2])
                        if (restored_status in {"active", "non_renewing"} and
                                (not period_end or datetime.fromisoformat(period_end) <= current.astimezone(UTC))):
                            restored_status = "expired"
                        if restored_status in _STATUSES and restored_status != "disputed":
                            connection.execute(
                                """UPDATE subscriptions SET status = ?, status_before_dispute = NULL,
                                   last_provider_event_at = coalesce(?, last_provider_event_at),
                                   updated_at = ? WHERE provider = ? AND provider_subscription_id = ?""",
                                (restored_status, provider_event_time, received_at, provider, payment[0]),
                            )
                connection.commit()
                return True
        except sqlite3.Error as exc:
            raise BillingStoreError("Could not persist dispute resolution") from exc

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

    def has_active_feature(
        self, user_id: str, feature: str, plan_features: Mapping[str, Collection[str]],
        *, now: datetime | None = None,
    ) -> bool:
        """Resolve feature access only from active, unexpired subscriptions."""
        if (not isinstance(user_id, str) or not user_id.strip() or
                not isinstance(feature, str) or not _FEATURE_ID.fullmatch(feature)):
            return False
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("entitlement check time must include a timezone")
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """SELECT plan_id, status, current_period_end FROM subscriptions
                       WHERE user_id = ? AND status IN ('active', 'non_renewing')""",
                    (user_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise BillingStoreError("Could not read subscription entitlements") from exc
        for plan_id, status, period_end in rows:
            if feature not in plan_features.get(plan_id, ()):
                continue
            if status not in {"active", "non_renewing"} or not period_end:
                continue
            try:
                expiry = datetime.fromisoformat(period_end)
            except ValueError as exc:
                raise BillingStoreError("Subscription contains an invalid period end") from exc
            if expiry.tzinfo is None:
                raise BillingStoreError("Subscription period end is missing its timezone")
            if expiry.astimezone(UTC) > current.astimezone(UTC):
                return True
        return False

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
