from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
