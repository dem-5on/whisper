"""Owner-only file-backed feature entitlements for the hosted prototype."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any, Collection, Mapping

from .billing import BillingStore, BillingStoreError


FEATURE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


class EntitlementConfigError(RuntimeError):
    """The service entitlement database is missing permissions or malformed."""


class EntitlementStore:
    """Read feature access from an owner-only JSON file, failing closed."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def has_feature(self, user_id: str, feature: str, *, now: datetime | None = None) -> bool:
        if not FEATURE_ID.fullmatch(feature):
            return False
        entitlements = _read_entitlements(self.path)
        user_features = entitlements.get(user_id, {})
        record = user_features.get(feature)
        if not isinstance(record, dict):
            return False
        expires_at = record.get("expires_at")
        if expires_at is None:
            return True
        try:
            expiry = datetime.fromisoformat(expires_at)
        except (TypeError, ValueError) as exc:
            raise EntitlementConfigError("Entitlement database contains an invalid expiry") from exc
        if expiry.tzinfo is None:
            raise EntitlementConfigError("Entitlement expiry must include a timezone")
        current = now or datetime.now(UTC)
        return expiry.astimezone(UTC) > current.astimezone(UTC)


class SubscriptionEntitlementStore:
    """Combine operator grants with current billing-plan entitlements."""

    def __init__(
        self, manual: EntitlementStore, billing: BillingStore,
        plan_features: Mapping[str, Collection[str]],
    ) -> None:
        self.manual = manual
        self.billing = billing
        self.plan_features = {plan: tuple(features) for plan, features in plan_features.items()}

    def has_feature(self, user_id: str, feature: str, *, now: datetime | None = None) -> bool:
        if self.manual.has_feature(user_id, feature, now=now):
            return True
        try:
            return self.billing.has_active_feature(
                user_id, feature, self.plan_features, now=now,
            )
        except BillingStoreError as exc:
            raise EntitlementConfigError("Could not resolve subscription entitlements") from exc


def grant_feature(path: str | Path, user_id: str, feature: str, *, expires_at: str | None = None) -> None:
    _validate_identity(user_id, feature)
    normalized_expiry = _normalize_expiry(expires_at)
    location = Path(path)
    users = _read_entitlements(location)
    features = dict(users.get(user_id, {}))
    features[feature] = {"expires_at": normalized_expiry}
    users[user_id] = features
    _write_entitlements(location, users)


def revoke_feature(path: str | Path, user_id: str, feature: str) -> bool:
    _validate_identity(user_id, feature)
    location = Path(path)
    users = _read_entitlements(location)
    features = dict(users.get(user_id, {}))
    removed = feature in features
    features.pop(feature, None)
    if features:
        users[user_id] = features
    else:
        users.pop(user_id, None)
    if removed:
        _write_entitlements(location, users)
    return removed


def list_features(path: str | Path, user_id: str | None = None) -> dict[str, dict[str, Any]]:
    users = _read_entitlements(Path(path))
    if user_id is not None:
        return {user_id: dict(users.get(user_id, {}))}
    return {owner: dict(features) for owner, features in users.items()}


def _validate_identity(user_id: str, feature: str) -> None:
    if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 128:
        raise ValueError("user ID must be between 1 and 128 characters")
    if not isinstance(feature, str) or not FEATURE_ID.fullmatch(feature):
        raise ValueError("feature must be a lowercase identifier")


def _normalize_expiry(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("expiry must be an ISO-8601 timestamp with timezone, for example 2026-12-31T23:59:59Z") from exc
    if expiry.tzinfo is None:
        raise ValueError("expiry must include a timezone")
    expiry = expiry.astimezone(UTC)
    if expiry <= datetime.now(UTC):
        raise ValueError("expiry must be in the future")
    return expiry.isoformat(timespec="seconds")


def _read_entitlements(path: Path) -> dict[str, dict[str, Any]]:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise EntitlementConfigError("Entitlement database must be owner-only; set permissions to 0600")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except EntitlementConfigError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise EntitlementConfigError("Could not read entitlement database") from exc
    users = payload.get("users") if isinstance(payload, dict) else None
    if not isinstance(users, dict):
        raise EntitlementConfigError("Entitlement database must contain a users object")
    normalized: dict[str, dict[str, Any]] = {}
    for user_id, features in users.items():
        if (not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 128
                or not isinstance(features, dict)):
            raise EntitlementConfigError("Entitlement database contains an invalid user record")
        checked: dict[str, Any] = {}
        for feature, record in features.items():
            if not isinstance(feature, str) or not FEATURE_ID.fullmatch(feature) or not isinstance(record, dict):
                raise EntitlementConfigError("Entitlement database contains an invalid feature record")
            expires_at = record.get("expires_at")
            if expires_at is not None:
                if not isinstance(expires_at, str):
                    raise EntitlementConfigError("Entitlement database contains an invalid expiry")
                try:
                    expiry = datetime.fromisoformat(expires_at)
                except ValueError as exc:
                    raise EntitlementConfigError("Entitlement database contains an invalid expiry") from exc
                if expiry.tzinfo is None:
                    raise EntitlementConfigError("Entitlement expiry must include a timezone")
            checked[feature] = {"expires_at": expires_at}
        normalized[user_id] = checked
    return normalized


def _write_entitlements(path: Path, users: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        temporary.write_text(json.dumps({"users": users}, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
