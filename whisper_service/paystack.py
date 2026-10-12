"""Paystack checkout payloads, webhook verification, and billing events."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

from .billing import BillingStore, BillingStoreError

MAX_WEBHOOK_BYTES = 1_000_000
_SIGNATURE = re.compile(r"[0-9a-fA-F]{128}\Z")
_EVENT_NAME = re.compile(r"[a-z][a-z0-9_.]{0,127}\Z")
_PLAN_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z")


@dataclass(frozen=True)
class PaystackPlan:
    """A Wicx plan backed by a recurring or one-time Paystack payment."""

    plan_id: str
    paystack_code: str
    amount_minor: int | None = None
    currency: str | None = None
    features: tuple[str, ...] = ()
    billing_mode: str = "recurring"
    period_days: int = 30
    channels: tuple[str, ...] = ("mobile_money",)


def validate_checkout_request(
    *, user_id: str, email: str, plan_id: str, plans: Mapping[str, PaystackPlan],
) -> tuple[str | None, dict[str, Any]]:
    """Build a safe Paystack initialization payload from server-owned plans.

    The caller supplies the authenticated user ID, never one from the request
    body. All pricing and payment parameters come from the server's plan map.
    """
    if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 128:
        raise PaystackWebhookError("Authenticated account is invalid")
    if not isinstance(email, str) or len(email) > 254 or not _EMAIL.fullmatch(email):
        raise PaystackWebhookError("A valid billing email is required")
    if not isinstance(plan_id, str) or not _PLAN_ID.fullmatch(plan_id):
        raise PaystackWebhookError("Plan is invalid")
    plan = plans.get(plan_id)
    if (plan is None or plan.plan_id != plan_id or
            not isinstance(plan.amount_minor, int) or isinstance(plan.amount_minor, bool) or
            plan.amount_minor <= 0 or not isinstance(plan.currency, str) or
            not re.fullmatch(r"[A-Z]{3}", plan.currency) or
            plan.billing_mode not in {"recurring", "one_time"}):
        raise PaystackWebhookError("Plan is unavailable")
    metadata = {"wicx_user_id": user_id, "wicx_plan_id": plan_id}
    payload: dict[str, Any] = {
        "email": email,
        "metadata": metadata,
    }
    if plan.billing_mode == "recurring":
        if not isinstance(plan.paystack_code, str) or not plan.paystack_code.strip():
            raise PaystackWebhookError("Plan is unavailable")
        payload["plan"] = plan.paystack_code
        provider_plan = plan.paystack_code
    else:
        if (not isinstance(plan.period_days, int) or isinstance(plan.period_days, bool) or
                not 1 <= plan.period_days <= 366 or not plan.channels or
                any(channel not in {"mobile_money", "card", "bank", "ussd", "qr", "eft"}
                    for channel in plan.channels)):
            raise PaystackWebhookError("Plan is unavailable")
        payload.update({
            "amount": plan.amount_minor,
            "currency": plan.currency,
            "channels": list(plan.channels),
        })
        provider_plan = None
    return provider_plan, payload


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
    occurred_at: str | None = None


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
    return VerifiedPaystackEvent(
        name=name, data=data, digest=hashlib.sha256(body).hexdigest(),
        occurred_at=_event_timestamp(payload, data),
    )


def apply_paystack_subscription_event(
    store: BillingStore, event: VerifiedPaystackEvent,
    plans: Mapping[str, PaystackPlan] | None = None,
) -> bool:
    """Apply a verified subscription lifecycle event to the billing ledger.

    The first successful subscription charge must resolve to a durable checkout
    and account mapping. Refund and dispute events are linked by the original
    transaction reference; unknown references never affect account access.
    """
    data = event.data
    if event.name in {"refund.processed", "charge.dispute.create", "charge.dispute.remind"}:
        if event.name == "refund.processed":
            # A pending refund may still fail. Revoke only once Paystack confirms
            # the refund was processed.
            if data.get("status") != "processed":
                return False
            reference = _payment_reference(data)
            status = "refunded"
        else:
            reference = _payment_reference(data)
            status = "disputed"
        if reference is None:
            return False
        try:
            return store.apply_payment_status_event(
                provider="paystack", event_id=event.digest,
                transaction_reference=reference, status=status,
                event_time=event.occurred_at,
            )
        except (ValueError, BillingStoreError) as exc:
            raise PaystackWebhookError("Verified payment lifecycle event could not be applied") from exc

    if event.name == "charge.dispute.resolve":
        # Paystack documents `declined` as rejecting the dispute (merchant wins)
        # and `merchant-accepted` as accepting it. Unknown/incomplete outcomes
        # intentionally leave the existing hold untouched.
        if data.get("status") != "resolved":
            return False
        resolution = data.get("resolution")
        reference = _payment_reference(data)
        if (not isinstance(resolution, str) or
                resolution not in {"merchant-accepted", "declined"} or reference is None):
            return False
        try:
            return store.apply_dispute_resolution_event(
                provider="paystack", event_id=event.digest,
                transaction_reference=reference, resolution=resolution,
                event_time=event.occurred_at,
            )
        except (ValueError, BillingStoreError) as exc:
            raise PaystackWebhookError("Verified dispute resolution could not be applied") from exc

    subscription = data.get("subscription")
    if not isinstance(subscription, dict):
        subscription = {}
    subscription_id = _first_string(
        subscription.get("subscription_code"), data.get("subscription_code")
    )
    manual_checkout = data.get("_wicx_manual_checkout")
    if (subscription_id is None and isinstance(manual_checkout, dict) and
            isinstance(manual_checkout.get("reference"), str)):
        subscription_id = f"once-{manual_checkout['reference']}"
    if subscription_id is None:
        return False

    current = store.get_subscription("paystack", subscription_id)
    if event.name == "charge.success":
        if data.get("status") != "success":
            return False
        if event.occurred_at is None:
            # Paystack charge events include created_at/paid_at; without one we
            # cannot safely distinguish a late stale charge from a renewal.
            return False
        transaction_reference = _payment_reference(data)
        if transaction_reference is None:
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
        if isinstance(manual_checkout, dict) and (
                plan.billing_mode != "one_time" or
                manual_checkout.get("plan_id") != plan_id or
                manual_checkout.get("reference") != transaction_reference):
            return False
        # A successful renewal must not silently clear an unresolved dispute.
        status = "disputed" if current and current["status"] == "disputed" else "active"
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
        else:
            # subscription.create and invoice.update are lifecycle notices, not
            # independently verified charges. Only charge.success (verified by
            # the HTTP adapter against Paystack's transaction API) may grant or
            # restore paid access.
            status = current["status"]
    else:
        return False

    if current and current["status"] == "disputed":
        # Keep the account hold through renewals and routine subscription events.
        status = "disputed"
    elif (event.name != "charge.success" and current and
          current["status"] in {"attention", "refunded", "cancelled", "completed", "expired"} and
          status in {"active", "non_renewing"}):
        # A lifecycle notice cannot restore paid access, even if it is newer.
        # Only the verified charge.success path may do that.
        status = current["status"]

    period_end = _first_string(
        subscription.get("next_payment_date"), data.get("next_payment_date"),
        data.get("period_end"),
    ) or (current["current_period_end"] if current else None)
    if event.name == "charge.success" and isinstance(manual_checkout, dict):
        try:
            period_days = manual_checkout.get("period_days")
            if not isinstance(period_days, int) or isinstance(period_days, bool) or not 1 <= period_days <= 366:
                return False
            paid_at = datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
            period_end = (paid_at + timedelta(days=period_days)).astimezone(UTC).isoformat(timespec="seconds")
        except (AttributeError, TypeError, ValueError):
            return False
    try:
        return store.apply_verified_event(
            provider="paystack", event_id=event.digest,
            subscription_id=subscription_id, user_id=user_id, plan_id=plan_id,
            status=status, current_period_end=period_end,
            transaction_reference=transaction_reference if event.name == "charge.success" else None,
            event_time=event.occurred_at,
        )
    except (ValueError, BillingStoreError) as exc:
        raise PaystackWebhookError("Verified subscription event could not be applied") from exc


def _reject_nonstandard_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant: {value}")


def _first_string(*values: Any) -> str | None:
    return next((value for value in values if isinstance(value, str) and value), None)


def _payment_reference(data: Mapping[str, Any]) -> str | None:
    """Read references across Paystack charge, refund, and dispute envelopes."""
    transaction = data.get("transaction")
    if not isinstance(transaction, dict):
        transaction = {}
    return _first_string(
        data.get("transaction_reference"), data.get("reference"),
        transaction.get("reference"),
    )


def _event_timestamp(payload: Mapping[str, Any], data: Mapping[str, Any]) -> str | None:
    """Normalize a timestamp embedded in the signed provider event payload."""
    subscription = data.get("subscription")
    if not isinstance(subscription, dict):
        subscription = {}
    for candidate in (
        data.get("resolvedAt"), data.get("paid_at"), data.get("created_at"),
        data.get("updated_at"), data.get("createdAt"), data.get("updatedAt"),
        subscription.get("updatedAt"), subscription.get("createdAt"),
        payload.get("createdAt"), payload.get("created_at"),
    ):
        if not isinstance(candidate, str) or not candidate:
            continue
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            continue
        return parsed.astimezone(UTC).isoformat(timespec="seconds")
    return None
