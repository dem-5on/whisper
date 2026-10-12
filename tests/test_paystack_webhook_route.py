from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from aiohttp.test_utils import TestClient, TestServer
except ImportError:
    TestClient = TestServer = None

from whisper_service.auth import TokenAuthenticator
from whisper_service.billing import BillingStore
from whisper_service.diarization_api import PAYSTACK_WEBHOOK_PATH, make_app
from whisper_service.paystack import PaystackPlan


class _FakeResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self.payload = payload

    async def json(self, content_type=None):
        return self.payload


class _ResponseContext:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return None


class _FakeSession:
    def __init__(self, response: _FakeResponse, calls: list[dict]) -> None:
        self.response = response
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _ResponseContext(self.response)


@unittest.skipIf(TestClient is None, "aiohttp server extra is not installed")
class PaystackWebhookRouteTests(unittest.IsolatedAsyncioTestCase):
    secret = "local-test-secret"
    plan = PaystackPlan("pro-monthly", "PLN_local", 1250, "GHS", ("diarization",))

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.store = BillingStore(Path(self.temp.name) / "billing.sqlite3")
        self.store.record_checkout(
            reference="checkout-123", user_id="account-owner", plan_id="pro-monthly",
            amount_minor=1250, currency="GHS",
        )
        app = make_app(
            TokenAuthenticator({}), None, None, billing_store=self.store,
            paystack_secret=self.secret, paystack_plans={"pro-monthly": self.plan},
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def _cleanup(self) -> None:
        await self.client.close()
        self.temp.cleanup()

    def _signed(self, name: str, data: dict) -> tuple[bytes, str]:
        body = json.dumps({"event": name, "data": data}, separators=(",", ":")).encode()
        signature = hmac.new(self.secret.encode(), body, hashlib.sha512).hexdigest()
        return body, signature

    async def _post(self, name: str, data: dict, signature: str | None = None):
        body, valid_signature = self._signed(name, data)
        return await self.client.post(
            PAYSTACK_WEBHOOK_PATH, data=body,
            headers={"x-paystack-signature": signature or valid_signature},
        )

    async def test_charge_verification_and_duplicate_delivery_are_safe(self) -> None:
        data = {
            "status": "success", "reference": "checkout-123", "amount": 1250,
            "created_at": "2026-10-10T10:00:00Z",
            "currency": "GHS", "metadata": {"wicx_user_id": "attacker", "wicx_plan_id": "fake"},
            "subscription": {"subscription_code": "SUB_123", "next_payment_date": "2030-01-01T00:00:00Z"},
        }
        verified = {"status": True, "data": {
            "status": "success", "reference": "checkout-123", "amount": 1250, "currency": "GHS",
        }}
        calls: list[dict] = []
        with patch("aiohttp.ClientSession", side_effect=lambda **_kwargs: _FakeSession(
            _FakeResponse(200, verified), calls,
        )):
            first = await self._post("charge.success", data)
            replay = await self._post("charge.success", data)

        self.assertEqual(first.status, 200)
        self.assertTrue((await first.json())["applied"])
        self.assertEqual(replay.status, 200)
        self.assertFalse((await replay.json())["applied"])
        self.assertEqual(len(calls), 2)
        subscription = self.store.get_subscription("paystack", "SUB_123")
        self.assertEqual(subscription["user_id"], "account-owner")
        self.assertEqual(subscription["plan_id"], "pro-monthly")

    async def test_provider_verification_http_failure_returns_retryable_error(self) -> None:
        data = {
            "status": "success", "reference": "checkout-123", "amount": 1250,
            "created_at": "2026-10-10T10:00:00Z",
            "currency": "GHS", "metadata": {}, "subscription": {"subscription_code": "SUB_123"},
        }
        calls: list[dict] = []
        with patch("aiohttp.ClientSession", side_effect=lambda **_kwargs: _FakeSession(
            _FakeResponse(503, {"status": False}), calls,
        )):
            response = await self._post("charge.success", data)
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"], "verification_unavailable")
        self.assertIsNone(self.store.get_subscription("paystack", "SUB_123"))

    async def test_provider_verification_mismatch_is_not_applied(self) -> None:
        data = {
            "status": "success", "reference": "checkout-123", "amount": 1250,
            "created_at": "2026-10-10T10:00:00Z",
            "currency": "GHS", "metadata": {}, "subscription": {"subscription_code": "SUB_123"},
        }
        mismatch = {"status": True, "data": {
            "status": "success", "reference": "checkout-123", "amount": 1, "currency": "GHS",
        }}
        calls: list[dict] = []
        with patch("aiohttp.ClientSession", side_effect=lambda **_kwargs: _FakeSession(
            _FakeResponse(200, mismatch), calls,
        )):
            response = await self._post("charge.success", data)
        self.assertEqual(response.status, 200)
        self.assertFalse((await response.json())["applied"])
        self.assertIsNone(self.store.get_subscription("paystack", "SUB_123"))

    async def test_bad_signature_is_rejected_and_processed_refund_revokes_access(self) -> None:
        charge = {
            "status": "success", "reference": "checkout-123", "amount": 1250,
            "created_at": "2026-10-10T10:00:00Z",
            "currency": "GHS", "metadata": {}, "subscription": {"subscription_code": "SUB_123"},
        }
        body, _signature = self._signed("charge.success", charge)
        bad_signature = "0" * 128
        response = await self.client.post(
            PAYSTACK_WEBHOOK_PATH, data=body,
            headers={"x-paystack-signature": bad_signature},
        )
        self.assertEqual(response.status, 401)
        self.assertIsNone(self.store.get_subscription("paystack", "SUB_123"))

        verified = {"status": True, "data": {
            "status": "success", "reference": "checkout-123", "amount": 1250, "currency": "GHS",
        }}
        calls: list[dict] = []
        with patch("aiohttp.ClientSession", side_effect=lambda **_kwargs: _FakeSession(
            _FakeResponse(200, verified), calls,
        )):
            paid = await self._post("charge.success", charge)
        self.assertTrue((await paid.json())["applied"])

        refund = await self._post("refund.processed", {
            "status": "processed", "transaction_reference": "checkout-123",
        })
        self.assertEqual(refund.status, 200)
        self.assertTrue((await refund.json())["applied"])
        self.assertEqual(self.store.get_subscription("paystack", "SUB_123")["status"], "refunded")


if __name__ == "__main__":
    unittest.main()
