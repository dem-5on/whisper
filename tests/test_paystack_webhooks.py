from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest

from whisper_service.billing import BillingStore
from whisper_service.paystack import (
    MAX_WEBHOOK_BYTES,
    PaystackPlan,
    PaystackWebhookError,
    VerifiedPaystackEvent,
    apply_paystack_subscription_event,
    validate_checkout_request,
    verify_paystack_webhook,
)


class PaystackWebhookVerificationTests(unittest.TestCase):
    secret = "test-secret-from-environment"

    def sign(self, body: bytes) -> str:
        return hmac.new(self.secret.encode(), body, hashlib.sha512).hexdigest()

    def test_accepts_correct_signature_and_decodes_event(self) -> None:
        body = json.dumps({"event": "subscription.create", "data": {"id": 123}}).encode()
        event = verify_paystack_webhook(body, self.sign(body), self.secret)
        self.assertEqual(event.name, "subscription.create")
        self.assertEqual(event.data, {"id": 123})
        self.assertEqual(event.digest, hashlib.sha256(body).hexdigest())

    def test_rejects_missing_malformed_or_incorrect_signature(self) -> None:
        body = b'{"event":"charge.success","data":{}}'
        for signature in (None, "", "abc", "g" * 128, "0" * 128):
            with self.subTest(signature=signature), self.assertRaises(PaystackWebhookError):
                verify_paystack_webhook(body, signature, self.secret)

    def test_rejects_unconfigured_secret_and_body_size_violations(self) -> None:
        body = b'{"event":"charge.success","data":{}}'
        signature = self.sign(body)
        with self.assertRaisesRegex(PaystackWebhookError, "not configured"):
            verify_paystack_webhook(body, signature, "")
        with self.assertRaisesRegex(PaystackWebhookError, "empty or too large"):
            verify_paystack_webhook(b"", signature, self.secret)
        too_large = b" " * (MAX_WEBHOOK_BYTES + 1)
        with self.assertRaisesRegex(PaystackWebhookError, "empty or too large"):
            verify_paystack_webhook(too_large, self.sign(too_large), self.secret)

    def test_signed_but_malformed_json_or_envelopes_are_rejected(self) -> None:
        for body in (
            b"not-json",
            b"[]",
            b'{"event":"bad name","data":{}}',
            b'{"event":"charge.success","data":null}',
            b'{"event":"charge.success","data":{"amount":NaN}}',
        ):
            with self.subTest(body=body), self.assertRaises(PaystackWebhookError):
                verify_paystack_webhook(body, self.sign(body), self.secret)

    def test_changed_payload_cannot_reuse_original_signature(self) -> None:
        original = b'{"event":"charge.success","data":{"status":"success"}}'
        modified = b'{"event":"charge.success","data":{"status":"failed"}}'
        with self.assertRaisesRegex(PaystackWebhookError, "signature is invalid"):
            verify_paystack_webhook(modified, self.sign(original), self.secret)


class PaystackCheckoutPayloadTests(unittest.TestCase):
    def test_plan_code_and_account_metadata_are_server_owned(self) -> None:
        plan = PaystackPlan("pro-monthly", "PLN_server-plan-code", 1250, "GHS")
        provider_plan, payload = validate_checkout_request(
            user_id="authenticated-user",
            email="payer@example.com",
            plan_id="pro-monthly",
            plans={"pro-monthly": plan},
        )
        self.assertEqual(provider_plan, "PLN_server-plan-code")
        self.assertEqual(payload["plan"], "PLN_server-plan-code")
        self.assertNotIn("amount", payload)
        self.assertEqual(payload["metadata"], {
            "wicx_user_id": "authenticated-user",
            "wicx_plan_id": "pro-monthly",
        })

    def test_unknown_plan_and_invalid_email_are_rejected(self) -> None:
        plans = {"pro-monthly": PaystackPlan("pro-monthly", "PLN_server-plan-code")}
        with self.assertRaisesRegex(PaystackWebhookError, "unavailable"):
            validate_checkout_request(
                user_id="authenticated-user", email="payer@example.com",
                plan_id="made-up", plans=plans,
            )
        with self.assertRaisesRegex(PaystackWebhookError, "email"):
            validate_checkout_request(
                user_id="authenticated-user", email="not-an-email",
                plan_id="pro-monthly", plans=plans,
            )


class PaystackSubscriptionEventTests(unittest.TestCase):
    plans = {"pro-monthly": PaystackPlan("pro-monthly", "PLN_server-plan-code", 1250, "GHS")}

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = BillingStore(Path(self.temp.name) / "billing.sqlite3")

    def event(self, name: str, digest: str, data: dict) -> VerifiedPaystackEvent:
        return VerifiedPaystackEvent(name=name, data=data, digest=digest * 64)

    def test_first_successful_subscription_charge_creates_account_mapping(self) -> None:
        event = self.event("charge.success", "a", {
            "status": "success",
            "amount": 1250,
            "currency": "GHS",
            "plan": "PLN_server-plan-code",
            "metadata": {"wicx_user_id": "user-1", "wicx_plan_id": "pro-monthly"},
            "subscription": {
                "subscription_code": "SUB_1",
                "status": "active",
                "next_payment_date": "2026-11-11T00:00:00Z",
            },
        })
        self.assertTrue(apply_paystack_subscription_event(self.store, event, self.plans))
        self.assertEqual(self.store.get_subscription("paystack", "SUB_1")["user_id"], "user-1")
        self.assertFalse(apply_paystack_subscription_event(self.store, event, self.plans))

    def test_successful_charge_with_wrong_amount_or_currency_is_not_applied(self) -> None:
        event = self.event("charge.success", "e", {
            "status": "success", "amount": 1, "currency": "USD",
            "metadata": {"wicx_user_id": "user-1", "wicx_plan_id": "pro-monthly"},
            "subscription": {"subscription_code": "SUB_bad"},
        })
        self.assertFalse(apply_paystack_subscription_event(self.store, event, self.plans))
        self.assertIsNone(self.store.get_subscription("paystack", "SUB_bad"))

    def test_missing_checkout_metadata_never_guesses_owner_from_email(self) -> None:
        event = self.event("charge.success", "b", {
            "status": "success", "amount": 1250, "currency": "GHS",
            "customer": {"email": "user@example.com"},
            "subscription": {"subscription_code": "SUB_2"},
        })
        self.assertFalse(apply_paystack_subscription_event(self.store, event, self.plans))
        self.assertIsNone(self.store.get_subscription("paystack", "SUB_2"))

    def test_subscription_events_update_only_previously_mapped_account(self) -> None:
        initial = self.event("charge.success", "c", {
            "status": "success",
            "amount": 1250,
            "currency": "GHS",
            "metadata": {"wicx_user_id": "user-1", "wicx_plan_id": "pro-monthly"},
            "subscription": {"subscription_code": "SUB_3", "next_payment_date": "2026-11-11T00:00:00Z"},
        })
        apply_paystack_subscription_event(self.store, initial, self.plans)
        canceled = self.event("subscription.not_renew", "d", {
            "subscription_code": "SUB_3", "status": "non-renewing",
        })
        self.assertTrue(apply_paystack_subscription_event(self.store, canceled, self.plans))
        record = self.store.get_subscription("paystack", "SUB_3")
        self.assertEqual(record["status"], "non_renewing")
        self.assertEqual(record["current_period_end"], "2026-11-11T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
