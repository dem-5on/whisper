"""Subscription plans and bundled usage allowances for hosted Wicx features.

One subscription bundles everything: recording, cloud transcription,
rewriting, lecture/meeting analysis, and diarization all draw from the same
allowance. There are no per-feature payments; an exhausted allowance only
pauses paid analysis until the allowance renews or the tier is upgraded.

Values here are working placeholders the operator edits later through the
admin portal (or today through ``WICX_PLANS_FILE``). Paystack billing plans
from ``WHISPER_PAYSTACK_PLANS`` keep working unchanged and take precedence
where both define the same plan id.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Collection, Mapping


class PlanConfigError(RuntimeError):
    """A subscription plan definition is missing or malformed."""


@dataclass(frozen=True)
class Plan:
    """One subscription tier with its bundled allowance."""

    id: str
    name: str
    features: tuple[str, ...]
    cloud_minutes: int | None
    analysis_minutes_per_stage: int = 1
    period_days: int = 30


CLOUD_FEATURES: tuple[str, ...] = (
    "cloud_transcription",
    "cloud_rewrite",
    "diarization",
    "lecture_analysis",
    "meeting_analysis",
    "translation",
)

#: Analysis request kind -> entitlement feature that gates it.
KIND_FEATURE: dict[str, str] = {
    "notes": "lecture_analysis",
    "topics": "lecture_analysis",
    "terms": "lecture_analysis",
    "questions": "lecture_analysis",
    "translation": "translation",
    "summary": "meeting_analysis",
    "actions": "meeting_analysis",
    "decisions": "meeting_analysis",
}

ANALYSIS_KINDS: tuple[str, ...] = tuple(KIND_FEATURE)

DEFAULT_PLANS: dict[str, Plan] = {
    "free": Plan(
        id="free",
        name="Free",
        features=(),
        cloud_minutes=0,
        analysis_minutes_per_stage=1,
        period_days=30,
    ),
    "pro": Plan(
        id="pro",
        name="Pro",
        features=CLOUD_FEATURES,
        cloud_minutes=600,
        analysis_minutes_per_stage=1,
        period_days=30,
    ),
}


def load_plans(source: str | Path | None = None) -> dict[str, Plan]:
    """Load plans from a JSON file, falling back to built-in placeholders.

    Set ``WICX_PLANS_FILE`` (or pass ``source``) to point at the operator
    maintained file; the future admin portal writes the same shape.
    """
    path_value = source if source is not None else os.environ.get("WICX_PLANS_FILE", "")
    if not str(path_value).strip():
        return dict(DEFAULT_PLANS)
    return _read_plans_file(Path(str(path_value)))


def _read_plans_file(path: Path) -> dict[str, Plan]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PlanConfigError(f"Plans file not found: {path}") from exc
    except (OSError, ValueError) as exc:
        raise PlanConfigError(f"Plans file is unreadable: {path}") from exc
    if not isinstance(raw, dict) or not raw:
        raise PlanConfigError("Plans file must be a non-empty JSON object")
    plans: dict[str, Plan] = {}
    for plan_id, details in raw.items():
        plans[plan_id] = _parse_plan(plan_id, details)
    return plans


def _parse_plan(plan_id: object, details: object) -> Plan:
    import re

    if not isinstance(plan_id, str) or not plan_id.strip() or len(plan_id) > 64:
        raise PlanConfigError("Plan ids must be non-empty strings")
    if not isinstance(details, dict):
        raise PlanConfigError(f"Plan '{plan_id}' must be a JSON object")
    name = details.get("name", plan_id)
    features = details.get("features", [])
    cloud_minutes = details.get("cloud_minutes")
    per_stage = details.get("analysis_minutes_per_stage", 1)
    period_days = details.get("period_days", 30)
    if (not isinstance(name, str) or not name.strip()
            or not isinstance(features, list)
            or any(not isinstance(f, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", f) for f in features)
            or (cloud_minutes is not None and (
                not isinstance(cloud_minutes, int) or isinstance(cloud_minutes, bool)
                or cloud_minutes < 0 or cloud_minutes > 10_000_000))
            or (not isinstance(per_stage, int) or isinstance(per_stage, bool)
                or per_stage < 1 or per_stage > 1_000)
            or (not isinstance(period_days, int) or isinstance(period_days, bool)
                or not 1 <= period_days <= 366)):
        raise PlanConfigError(f"Plan '{plan_id}' has invalid fields")
    return Plan(
        id=plan_id,
        name=name.strip(),
        features=tuple(features),
        cloud_minutes=cloud_minutes,
        analysis_minutes_per_stage=per_stage,
        period_days=period_days,
    )


def to_billing_mapping(plans: Mapping[str, Plan]) -> dict[str, tuple[tuple[str, ...], int | None]]:
    """Adapt plans to the (features, allowance_minutes) shape billing expects."""
    return {
        plan_id: (plan.features, plan.cloud_minutes)
        for plan_id, plan in plans.items()
    }


def plan_features(plans: Mapping[str, Plan]) -> dict[str, tuple[str, ...]]:
    """Adapt plans to the plan-features mapping entitlements expect."""
    return {plan_id: plan.features for plan_id, plan in plans.items()}


def features_for_kinds(kinds: Collection[str]) -> set[str]:
    """Return the entitlement features covering the given analysis kinds."""
    unknown = [kind for kind in kinds if kind not in KIND_FEATURE]
    if unknown:
        raise PlanConfigError(f"Unknown analysis kinds: {', '.join(sorted(unknown))}")
    return {KIND_FEATURE[kind] for kind in kinds}
