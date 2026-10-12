from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest

from whisper_service.billing import BillingStore, BillingStoreError


class BillingStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "billing.sqlite3"
        self.store = BillingStore(self.path)

    def test_database_is_created_with_private_permissions(self) -> None:
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_verified_subscription_event_is_durable_and_replay_safe(self) -> None:
        values = {
            "provider": "paystack",
            "event_id": "evt-001",
            "subscription_id": "SUB_123",
            "user_id": "account-abc",
            "plan_id": "pro-monthly",
            "status": "active",
            "current_period_end": "2026-11-11T00:00:00+00:00",
            "now": datetime(2026, 10, 11, tzinfo=UTC),
        }
        self.assertTrue(self.store.apply_verified_event(**values))
        self.assertFalse(self.store.apply_verified_event(**values))

        # A fresh store object sees persisted state, not process-local memory.
        persisted = BillingStore(self.path).get_subscription("paystack", "SUB_123")
        self.assertEqual(persisted, {
            "provider": "paystack",
            "subscription_id": "SUB_123",
            "user_id": "account-abc",
            "plan_id": "pro-monthly",
            "status": "active",
            "current_period_end": "2026-11-11T00:00:00+00:00",
            "updated_at": "2026-10-11T00:00:00+00:00",
        })

    def test_distinct_event_updates_existing_subscription(self) -> None:
        base = {
            "provider": "paystack", "subscription_id": "SUB_123",
            "user_id": "account-abc", "plan_id": "pro-monthly",
            "current_period_end": None,
        }
        self.store.apply_verified_event(**base, event_id="evt-001", status="active")
        self.store.apply_verified_event(**base, event_id="evt-002", status="non_renewing")
        current = self.store.get_subscription("paystack", "SUB_123")
        self.assertEqual(current["status"], "non_renewing")

    def test_invalid_event_data_is_rejected_without_writing(self) -> None:
        base = {
            "provider": "paystack", "event_id": "evt-001",
            "subscription_id": "SUB_123", "user_id": "account-abc",
            "plan_id": "pro-monthly", "status": "active",
            "current_period_end": "2026-11-11T00:00:00",
        }
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.store.apply_verified_event(**base)
        base["current_period_end"] = None
        base["status"] = "unknown"
        with self.assertRaisesRegex(ValueError, "status"):
            self.store.apply_verified_event(**base)
        self.assertIsNone(self.store.get_subscription("paystack", "SUB_123"))

    def test_existing_world_readable_database_fails_closed(self) -> None:
        path = Path(self.temp.name) / "unsafe.sqlite3"
        path.touch(mode=0o644)
        os.chmod(path, 0o644)
        with self.assertRaises(BillingStoreError):
            BillingStore(path)

    def test_existing_billing_database_is_migrated_for_event_ordering(self) -> None:
        path = Path(self.temp.name) / "legacy.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute("""CREATE TABLE subscriptions (
                provider TEXT NOT NULL, provider_subscription_id TEXT NOT NULL,
                user_id TEXT NOT NULL, plan_id TEXT NOT NULL, status TEXT NOT NULL,
                current_period_end TEXT, updated_at TEXT NOT NULL,
                PRIMARY KEY (provider, provider_subscription_id))""")
            connection.execute("""CREATE TABLE webhook_events (
                provider TEXT NOT NULL, event_id TEXT NOT NULL, received_at TEXT NOT NULL,
                PRIMARY KEY (provider, event_id))""")
            connection.execute("""CREATE TABLE checkout_attempts (
                reference TEXT PRIMARY KEY, user_id TEXT NOT NULL, plan_id TEXT NOT NULL,
                amount_minor INTEGER NOT NULL, currency TEXT NOT NULL, created_at TEXT NOT NULL)""")
        os.chmod(path, 0o600)
        migrated = BillingStore(path)
        with sqlite3.connect(path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(subscriptions)")}
        self.assertIn("last_provider_event_at", columns)
        self.assertIn("status_before_dispute", columns)
        self.assertIsNone(migrated.get_subscription("paystack", "missing"))

    def test_checkout_reference_is_durable_and_account_linked(self) -> None:
        self.store.record_checkout(
            reference="checkout-123", user_id="account-abc", plan_id="pro-monthly",
            amount_minor=1250, currency="GHS",
            now=datetime(2026, 10, 11, tzinfo=UTC),
        )
        self.assertEqual(BillingStore(self.path).get_checkout("checkout-123"), {
            "reference": "checkout-123", "user_id": "account-abc",
            "plan_id": "pro-monthly", "amount_minor": 1250,
            "currency": "GHS", "billing_mode": "recurring", "period_days": 30,
            "created_at": "2026-10-11T00:00:00+00:00",
        })
        with self.assertRaises(BillingStoreError):
            self.store.record_checkout(
                reference="checkout-123", user_id="another-account", plan_id="pro-monthly",
                amount_minor=1250, currency="GHS",
            )

    def test_user_subscription_summaries_are_scoped_and_hide_provider_identifiers(self) -> None:
        self.store.apply_verified_event(
            provider="paystack", event_id="evt-owner", subscription_id="secret-provider-id",
            user_id="account-owner", plan_id="pro-monthly", status="active",
            current_period_end="2030-01-01T00:00:00Z",
        )
        subscriptions = self.store.list_user_subscriptions("account-owner")
        self.assertEqual(len(subscriptions), 1)
        self.assertEqual(subscriptions[0]["plan_id"], "pro-monthly")
        self.assertNotIn("secret-provider-id", str(subscriptions))
        self.assertEqual(self.store.list_user_subscriptions("another-account"), [])

    def test_only_current_recurring_subscription_can_be_managed(self) -> None:
        now = datetime(2029, 1, 1, tzinfo=UTC)
        self.store.apply_verified_event(
            provider="paystack", event_id="recurring", subscription_id="SUB_RECURRING",
            user_id="account-owner", plan_id="pro-monthly", status="active",
            current_period_end="2030-01-01T00:00:00Z", now=now,
        )
        self.store.apply_verified_event(
            provider="paystack", event_id="one-time", subscription_id="once-ref-123",
            user_id="account-owner", plan_id="pro-one-time", status="active",
            current_period_end="2030-01-01T00:00:00Z", now=now,
        )
        self.assertEqual(
            self.store.get_manageable_subscription("account-owner", "pro-monthly", now=now),
            "SUB_RECURRING",
        )
        self.assertIsNone(self.store.get_manageable_subscription(
            "other-account", "pro-monthly", now=now,
        ))
        self.assertIsNone(self.store.get_manageable_subscription(
            "account-owner", "pro-one-time", now=now,
        ))
        self.assertIsNone(self.store.get_manageable_subscription(
            "account-owner", "pro-monthly", now=datetime(2030, 1, 1, tzinfo=UTC),
        ))


if __name__ == "__main__":
    unittest.main()
