"""Paystack checkout payloads, webhook verification, and billing events."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import re
from typing import Any, Mapping

from .billing import BillingStore, BillingStoreError

MAX_WEBHOOK_BYTES = 1_000_000
_SIGNATURE = re.compile(r"[0-9a-fA-F]{128}\Z")
_EVENT_NAME = re.compile(r"[a-z][a-z0-9_.]{0,127}\Z")
_PLAN_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z")


@dataclass(frozen=True)
class PaystackPlan:
    """A Wicx plan mapped to a Paystack recurring plan code."""

    plan_id: str
    paystack_code: str
    amount_minor: int | None = None
    currency: str | None = None


def validate_checkout_request(
    *, user_id: str, email: str, plan_id: str, plans: Mapping[str, PaystackPlan],
) -> tuple[str, dict[str, Any]]:
    """Build a safe Paystack initialization payload from server-owned plans.

    The caller supplies the authenticated user ID, never one from the request
    body. Amounts are not accepted here: Paystack's configured plan code
    determines the recurring price.
    """
    if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 128:
        raise PaystackWebhookError("Authenticated account is invalid")
    if not isinstance(email, str) or len(email) > 254 or not _EMAIL.fullmatch(email):
        raise PaystackWebhookError("A valid billing email is required")
    if not isinstance(plan_id, str) or not _PLAN_ID.fullmatch(plan_id):
        raise PaystackWebhookError("Plan is invalid")
    plan = plans.get(plan_id)
    if (plan is None or plan.plan_id != plan_id or not plan.paystack_code or
            not isinstance(plan.amount_minor, int) or isinstance(plan.amount_minor, bool) or
            plan.amount_minor <= 0 or not isinstance(plan.currency, str) or
            not re.fullmatch(r"[A-Z]{3}", plan.currency)):
        raise PaystackWebhookError("Plan is unavailable")
    metadata = {"wicx_user_id": user_id, "wicx_plan_id": plan_id}
    return plan.paystack_code, {
        "email": email,
        "plan": plan.paystack_code,
        "metadata": metadata,
    }


def verified_transaction_matches(
    response: Any, *, reference: str, amount_minor: int, currency: str,
) -> bool:
    """Accept only Paystack's successful verification response for this charge."""
    if not isinstance(response, dict) or response.get("status") is not True:
        return False
    data = response.get("data")
    if not isinstance(data, dict):
        return False
    amount = data.get("amount")
    return (
        data.get("status") == "success"
        and data.get("reference") == reference
        and isinstance(amount, int)
        and not isinstance(amount, bool)
        and amount == amount_minor
        and data.get("currency") == currency
    )


class PaystackWebhookError(ValueError):
    """A webhook body, signature, or event envelope is invalid."""


@dataclass(frozen=True)
class VerifiedPaystackEvent:
    """A verified event envelope; the raw request buffer isn't retained."""

    name: str
    data: dict[str, Any]
    digest: str


def verify_paystack_webhook(body: bytes, signature: str | None, secret: str) -> VerifiedPaystackEvent:
    """Verify Paystack's HMAC-SHA512 header before decoding the JSON body."""
    if not isinstance(body, bytes) or not body or len(body) > MAX_WEBHOOK_BYTES:
        raise PaystackWebhookError("Webhook body is empty or too large")
    if not isinstance(secret, str) or not secret:
        raise PaystackWebhookError("Webhook verification is not configured")
    if not isinstance(signature, str) or not _SIGNATURE.fullmatch(signature):
        raise PaystackWebhookError("Webhook signature is missing or malformed")

    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(expected, signature.lower()):
        raise PaystackWebhookError("Webhook signature is invalid")

    try:
        payload = json.loads(body.decode("utf-8"), parse_constant=_reject_nonstandard_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PaystackWebhookError("Webhook body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise PaystackWebhookError("Webhook body must be a JSON object")
    name = payload.get("event")
    data = payload.get("data")
    if not isinstance(name, str) or not _EVENT_NAME.fullmatch(name):
        raise PaystackWebhookError("Webhook event name is invalid")
    if not isinstance(data, dict):
        raise PaystackWebhookError("Webhook event data is invalid")
    return VerifiedPaystackEvent(name=name, data=data, digest=hashlib.sha256(body).hexdigest())


def apply_paystack_subscription_event(
    store: BillingStore, event: VerifiedPaystackEvent,
    plans: Mapping[str, PaystackPlan] | None = None,
) -> bool:
    """Apply a verified subscription lifecycle event to the billing ledger.

    The first successful subscription charge must carry the Wicx user and plan
    identifiers supplied as transaction metadata at checkout. Later lifecycle
    events resolve the already-stored subscription code. This only updates the
    billing ledger; it deliberately does not grant product entitlements yet.
    """
    data = event.data
    subscription = data.get("subscription")
    if not isinstance(subscription, dict):
        subscription = {}
    subscription_id = _first_string(
        subscription.get("subscription_code"), data.get("subscription_code")
    )
    if subscription_id is None:
        return False

    current = store.get_subscription("paystack", subscription_id)
    if event.name == "charge.success":
        if data.get("status") != "success":
            return False
        if current is None:
            metadata = data.get("metadata")
            if not isinstance(metadata, dict):
                return False
            user_id = metadata.get("wicx_user_id")
            plan_id = metadata.get("wicx_plan_id")
            if not isinstance(user_id, str) or not isinstance(plan_id, str):
                return False
        else:
            user_id, plan_id = current["user_id"], current["plan_id"]
        if plans is None:
            return False
        plan = plans.get(plan_id)
        amount = data.get("amount")
        currency = data.get("currency")
        if (plan is None or not isinstance(plan.amount_minor, int) or
                isinstance(plan.amount_minor, bool) or amount != plan.amount_minor or
                not isinstance(amount, int) or isinstance(amount, bool) or
                currency != plan.currency):
            return False
        status = "active"
    elif event.name in {"subscription.create", "subscription.not_renew", "subscription.disable",
                        "invoice.update", "invoice.payment_failed"}:
        if current is None:
            # No account-to-subscription mapping exists yet; never infer ownership
            # from an email address supplied by a payment event.
            return False
        user_id, plan_id = current["user_id"], current["plan_id"]
        if event.name == "subscription.not_renew":
            status = "non_renewing"
        elif event.name == "subscription.disable":
            status = "completed" if data.get("status") == "complete" else "cancelled"
        elif event.name == "invoice.payment_failed":
            status = "attention"
        elif data.get("paid") is True or data.get("status") == "success":
            status = "active"
        else:
            status = current["status"]
    else:
        return False

    period_end = _first_string(
        subscription.get("next_payment_date"), data.get("next_payment_date"),
        data.get("period_end"),
    ) or (current["current_period_end"] if current else None)
    try:
        return store.apply_verified_event(
            provider="paystack", event_id=event.digest,
            subscription_id=subscription_id, user_id=user_id, plan_id=plan_id,
            status=status, current_period_end=period_end,
        )
    except (ValueError, BillingStoreError) as exc:
        raise PaystackWebhookError("Verified subscription event could not be applied") from exc


def _reject_nonstandard_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant: {value}")


def _first_string(*values: Any) -> str | None:
    return next((value for value in values if isinstance(value, str) and value), None)
