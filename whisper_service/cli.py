"""Operator diagnostics for the hosted inference service."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from .auth import CombinedAuthenticator, TokenAuthenticator, add_user_token, revoke_user_tokens
from .accounts import AccountStore
from .billing import BillingStore
from .entitlements import grant_feature, list_features, revoke_feature
from .hardware import HardwareDetectionError, detect_hardware
from .paystack import PaystackPlan
from .sessions import ModelProfile, SessionLimits, SessionManager


def main() -> None:
    parser = argparse.ArgumentParser(prog="whisper-server", description="Whisper hosted-service diagnostics")
    subparsers = parser.add_subparsers(dest="command", required=True)
    hardware = subparsers.add_parser("hardware", help="detect the inference device/runtime")
    hardware.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    diarize = subparsers.add_parser("diarize-file", help="evaluate self-hosted speaker analysis on a local WAV")
    diarize.add_argument("--audio", type=Path, required=True)
    diarize.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    token = subparsers.add_parser("token", help="manage invite-only user credentials")
    token_commands = token.add_subparsers(dest="token_command", required=True)
    add = token_commands.add_parser("add", help="issue a user token; it is printed once")
    add.add_argument("--user", required=True)
    add.add_argument("--file", type=Path, default=_default_token_file())
    revoke = token_commands.add_parser("revoke", help="revoke all tokens for a user")
    revoke.add_argument("--user", required=True)
    revoke.add_argument("--file", type=Path, default=_default_token_file())
    entitlement = subparsers.add_parser("entitlement", help="manage paid feature access on this host")
    entitlement_commands = entitlement.add_subparsers(dest="entitlement_command", required=True)
    grant = entitlement_commands.add_parser("grant", help="grant one feature to a user")
    grant.add_argument("--user", required=True)
    grant.add_argument("--feature", choices=("cloud_transcription", "cloud_rewrite", "diarization"), required=True)
    grant.add_argument("--expires-at", help="optional ISO-8601 expiry, e.g. 2026-12-31T23:59:59Z")
    grant.add_argument("--file", type=Path, default=_default_entitlement_file())
    entitlement_revoke = entitlement_commands.add_parser("revoke", help="revoke one feature from a user")
    entitlement_revoke.add_argument("--user", required=True)
    entitlement_revoke.add_argument("--feature", choices=("cloud_transcription", "cloud_rewrite", "diarization"), required=True)
    entitlement_revoke.add_argument("--file", type=Path, default=_default_entitlement_file())
    entitlement_list = entitlement_commands.add_parser("list", help="list feature grants")
    entitlement_list.add_argument("--user")
    entitlement_list.add_argument("--file", type=Path, default=_default_entitlement_file())
    serve = subparsers.add_parser("serve", help="run the authenticated live WebSocket service")
    serve.add_argument("--host", default="127.0.0.1", help="bind address; use a TLS reverse proxy for public access")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    serve.add_argument("--model", default="base", help="server-owned faster-whisper model profile")
    serve.add_argument("--profile", default="default", help="client-visible allow-listed profile ID")
    serve.add_argument("--token-file", type=Path, default=_default_token_file())
    serve.add_argument("--entitlements-file", type=Path, default=_default_entitlement_file())
    serve.add_argument("--max-session-seconds", type=int, default=300)
    serve.add_argument("--max-concurrent", type=int, default=1)
    serve.add_argument("--max-starts-per-window", type=int, default=10)
    serve.add_argument("--rate-window-seconds", type=int, default=3600)
    serve.add_argument("--partial-interval", type=float, default=1.0)
    serve.add_argument("--rolling-window", type=int, default=8)
    serve.add_argument("--enable-diarization", action="store_true",
                       default=os.environ.get("WHISPER_ENABLE_DIARIZATION", "").lower() in {"1", "true", "yes"},
                       help="enable the optional authenticated self-hosted meeting/lecture speaker-analysis API")
    serve.add_argument("--enable-billing", action="store_true",
                       default=os.environ.get("WHISPER_ENABLE_BILLING", "").lower() in {"1", "true", "yes"},
                       help="enable the Paystack subscription webhook endpoint on the HTTP service")
    serve.add_argument("--enable-oauth", action="store_true",
                       default=os.environ.get("WHISPER_ENABLE_OAUTH", "").lower() in {"1", "true", "yes"},
                       help="enable Google/Apple account sign-in routes on the HTTP service")
    serve.add_argument("--enable-rewrite", action="store_true",
                       default=os.environ.get("WHISPER_ENABLE_REWRITE", "").lower() in {"1", "true", "yes"},
                       help="enable the subscription-gated OpenRouter text-rewrite API")
    serve.add_argument("--http-host", default=None, help="diarization API bind address (defaults to --host)")
    serve.add_argument("--http-port", type=int, default=8766)
    serve.add_argument("--diarization-device", choices=("cpu", "cuda"), default="cpu")
    serve.add_argument("--diarization-max-jobs", type=int, default=4)
    serve.add_argument("--diarization-workers", type=int, default=1)
    serve.add_argument("--diarization-temp-dir", type=Path, default=None,
                       help="private temporary directory for in-flight audio uploads")
    serve.add_argument("--billing-database", type=Path, default=_default_billing_database())
    args = parser.parse_args()
    try:
        if args.command == "hardware":
            print(json.dumps(detect_hardware(args.device).as_dict(), indent=2))
        elif args.command == "diarize-file":
            from .diarization import PyannoteDiarizationWorker

            if not args.audio.is_file():
                raise RuntimeError("The WAV file was not found")
            worker = PyannoteDiarizationWorker(device=args.device)
            worker.prepare()
            started = time.monotonic()
            turns = worker.diarize(args.audio)
            print(json.dumps({
                "audio": args.audio.name,
                "turns": turns,
                "processing_seconds": round(time.monotonic() - started, 3),
            }, indent=2))
        elif args.command == "token":
            if args.token_command == "add":
                issued = add_user_token(args.file, args.user)
                print(f"Token for {args.user} (copy it now; it cannot be recovered):\n{issued}")
            else:
                removed = revoke_user_tokens(args.file, args.user)
                print(f"Revoked {removed} token(s) for {args.user}.")
        elif args.command == "entitlement":
            if args.entitlement_command == "grant":
                grant_feature(args.file, args.user, args.feature, expires_at=args.expires_at)
                print(f"Granted {args.feature} to {args.user}.")
            elif args.entitlement_command == "revoke":
                removed = revoke_feature(args.file, args.user, args.feature)
                print(f"Revoked {args.feature} from {args.user}." if removed else "No matching grant existed.")
            else:
                print(json.dumps(list_features(args.file, args.user), indent=2))
        else:
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
            asyncio.run(_run_service(args))
    except (HardwareDetectionError, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from exc


async def _run_service(args: argparse.Namespace) -> None:
    from .entitlements import EntitlementStore, SubscriptionEntitlementStore
    from .server import serve_websocket
    from .worker import prepare_worker

    account_store = AccountStore(_default_account_database())
    authenticator = CombinedAuthenticator(TokenAuthenticator.from_file(args.token_file), account_store)
    manual_entitlements = EntitlementStore(args.entitlements_file)
    profile = ModelProfile(args.profile, args.model, ("cpu", "cuda"))
    selection, worker = await prepare_worker(
        args.device, profile,
        partial_interval_seconds=args.partial_interval,
        rolling_window_seconds=args.rolling_window,
    )
    logging.getLogger("whisper_service").info(
        "Inference ready: device=%s compute_type=%s model=%s reason=%s",
        selection.device, selection.compute_type, args.model, selection.reason,
    )
    limits = SessionLimits(
        max_audio_seconds=args.max_session_seconds,
        max_audio_bytes=args.max_session_seconds * 16_000 * 2,
        max_concurrent_global=args.max_concurrent,
        max_concurrent_per_user=1,
        max_starts_per_window=args.max_starts_per_window,
        rate_window_seconds=args.rate_window_seconds,
    )
    billing_store = None
    paystack_secret = os.environ.get("WHISPER_PAYSTACK_SECRET_KEY")
    paystack_plans = _paystack_plans_from_environment() if args.enable_billing else {}
    entitlements = manual_entitlements
    if args.enable_billing:
        if not paystack_secret:
            raise RuntimeError("WHISPER_PAYSTACK_SECRET_KEY is required when billing is enabled")
        billing_store = BillingStore(args.billing_database)
        entitlements = SubscriptionEntitlementStore(
            manual_entitlements, billing_store,
            {plan_id: plan.features for plan_id, plan in paystack_plans.items()},
        )
    manager = SessionManager(
        worker, (profile,), device=selection.device, limits=limits,
        usage_budget=(
            (lambda user_id: asyncio.to_thread(
                billing_store.cloud_usage_budget, user_id,
                {key: (plan.features, plan.cloud_minutes) for key, plan in paystack_plans.items()},
            )) if billing_store else None
        ),
        record_usage=(
            (lambda user_id, plan_id, period_end, milliseconds: asyncio.to_thread(
                billing_store.record_cloud_usage, user_id, plan_id, period_end, milliseconds,
            )) if billing_store else None
        ),
    )
    if not args.enable_diarization and not args.enable_billing and not args.enable_oauth and not args.enable_rewrite:
        await serve_websocket(authenticator, manager, entitlements=entitlements,
                              host=args.host, port=args.port)
        return

    from .diarization_api import serve_http
    diarization_jobs = None
    http_entitlements = entitlements if args.enable_billing else None
    if args.enable_diarization:
        from .diarization import PyannoteDiarizationWorker
        from .diarization_jobs import DiarizationJobManager

        diarizer = PyannoteDiarizationWorker(device=args.diarization_device)
        diarizer.prepare()
        diarization_jobs = DiarizationJobManager(
            diarizer, temp_dir=args.diarization_temp_dir,
            max_jobs=args.diarization_max_jobs, workers=args.diarization_workers,
        )
        http_entitlements = entitlements

    rewrite_model = None
    if args.enable_rewrite:
        from .rewrite import OpenRouterRewriteModel

        api_key = os.environ.get("WHISPER_OPENROUTER_API_KEY", "")
        models = tuple(item.strip() for item in os.environ.get("WHISPER_REWRITE_MODELS", "").split(",") if item.strip())
        if not api_key:
            raise RuntimeError("WHISPER_OPENROUTER_API_KEY is required when rewrite is enabled")
        if not models:
            raise RuntimeError("Set WHISPER_REWRITE_MODELS to an explicit comma-separated model allow-list")
        rewrite_model = OpenRouterRewriteModel(api_key, models)
        http_entitlements = entitlements

    oauth_transactions = None
    oauth_providers = None
    oauth_app_redirect = "wicx://auth/callback"
    if args.enable_oauth:
        from .oauth import OAuthTransactionStore

        oauth_transactions = OAuthTransactionStore(_default_oauth_database())
        oauth_providers = _oauth_providers_from_environment()
        oauth_app_redirect = os.environ.get("WHISPER_OAUTH_APP_REDIRECT", oauth_app_redirect)
        if not oauth_providers:
            raise RuntimeError("Configure at least one complete Google or Apple provider before enabling OAuth")
        if not oauth_app_redirect.startswith("wicx://"):
            raise RuntimeError("WHISPER_OAUTH_APP_REDIRECT must use the wicx:// scheme")

    await asyncio.gather(
        serve_websocket(authenticator, manager, entitlements=entitlements,
                        host=args.host, port=args.port),
        serve_http(authenticator, diarization_jobs,
                   entitlements=http_entitlements,
                   billing_store=billing_store,
                   paystack_secret=paystack_secret if args.enable_billing else None,
                   paystack_plans=paystack_plans if args.enable_billing else None,
                   oauth_transactions=oauth_transactions,
                   account_store=account_store if args.enable_oauth else None,
                   oauth_providers=oauth_providers,
                   rewrite_model=rewrite_model,
                   oauth_app_redirect=oauth_app_redirect,
                   host=args.http_host or args.host, port=args.http_port),
    )


def _default_token_file() -> Path:
    override = os.environ.get("WHISPER_TOKENS_FILE")
    if override:
        return Path(override)
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "whisper-server" / "tokens.json"


def _default_entitlement_file() -> Path:
    override = os.environ.get("WHISPER_ENTITLEMENTS_FILE")
    if override:
        return Path(override)
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "whisper-server" / "entitlements.json"


def _default_billing_database() -> Path:
    override = os.environ.get("WHISPER_BILLING_DATABASE")
    if override:
        return Path(override)
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return data_home / "whisper-server" / "billing.sqlite3"


def _default_account_database() -> Path:
    override = os.environ.get("WHISPER_ACCOUNTS_DATABASE")
    if override:
        return Path(override)
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return data_home / "whisper-server" / "accounts.sqlite3"


def _default_oauth_database() -> Path:
    override = os.environ.get("WHISPER_OAUTH_DATABASE")
    if override:
        return Path(override)
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return data_home / "whisper-server" / "oauth.sqlite3"


def _oauth_providers_from_environment() -> dict[str, "OAuthProviderConfig"]:
    from .oauth_flow import OAuthProviderConfig

    base_url = os.environ.get("WHISPER_OAUTH_BASE_URL", "").rstrip("/")
    if not base_url.startswith("https://"):
        raise RuntimeError("WHISPER_OAUTH_BASE_URL must be the public HTTPS origin for OAuth callbacks")
    providers: dict[str, OAuthProviderConfig] = {}
    google_id = os.environ.get("WHISPER_GOOGLE_CLIENT_ID")
    google_secret = os.environ.get("WHISPER_GOOGLE_CLIENT_SECRET")
    if google_id and google_secret:
        providers["google"] = OAuthProviderConfig(
            "google", google_id, f"{base_url}/v1/auth/oauth/google/callback",
            client_secret=google_secret,
        )
    apple_id = os.environ.get("WHISPER_APPLE_CLIENT_ID")
    apple_team = os.environ.get("WHISPER_APPLE_TEAM_ID")
    apple_key_id = os.environ.get("WHISPER_APPLE_KEY_ID")
    apple_key_file = os.environ.get("WHISPER_APPLE_PRIVATE_KEY_FILE")
    if apple_id and apple_team and apple_key_id and apple_key_file:
        key_path = Path(apple_key_file)
        try:
            apple_private_key = key_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError("Could not read WHISPER_APPLE_PRIVATE_KEY_FILE") from exc
        providers["apple"] = OAuthProviderConfig(
            "apple", apple_id, f"{base_url}/v1/auth/oauth/apple/callback",
            apple_team_id=apple_team, apple_key_id=apple_key_id,
            apple_private_key=apple_private_key,
        )
    return providers


def _paystack_plans_from_environment() -> dict[str, PaystackPlan]:
    raw = os.environ.get("WHISPER_PAYSTACK_PLANS", "")
    if not raw:
        return {}
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("WHISPER_PAYSTACK_PLANS must be a JSON object") from exc
    if not isinstance(configured, dict) or not configured:
        raise RuntimeError("WHISPER_PAYSTACK_PLANS must be a non-empty JSON object")
    plans: dict[str, PaystackPlan] = {}
    for plan_id, details in configured.items():
        features = details.get("features", []) if isinstance(details, dict) else None
        billing_mode = details.get("billing_mode", "recurring") if isinstance(details, dict) else None
        code = details.get("code") if isinstance(details, dict) else None
        period_days = details.get("period_days", 30) if isinstance(details, dict) else None
        channels = details.get("channels", ["mobile_money"]) if isinstance(details, dict) else None
        cloud_minutes = details.get("cloud_minutes") if isinstance(details, dict) else None
        if (not isinstance(plan_id, str) or not isinstance(details, dict) or
                billing_mode not in {"recurring", "one_time"} or
                (billing_mode == "recurring" and
                 (not isinstance(code, str) or not code.strip())) or
                not isinstance(details.get("amount_minor"), int) or
                isinstance(details.get("amount_minor"), bool) or details["amount_minor"] <= 0 or
                not isinstance(details.get("currency"), str) or
                not re.fullmatch(r"[A-Z]{3}", details["currency"]) or
                not isinstance(period_days, int) or isinstance(period_days, bool) or
                not 1 <= period_days <= 366 or
                (billing_mode == "one_time" and (
                    not isinstance(channels, list) or not channels or
                    any(channel not in {"mobile_money", "card", "bank", "ussd", "qr", "eft"}
                        for channel in channels)
                )) or
                not isinstance(features, list) or
                any(not isinstance(feature, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", feature)
                    for feature in features) or
                (cloud_minutes is not None and (
                    not isinstance(cloud_minutes, int) or isinstance(cloud_minutes, bool) or
                    cloud_minutes < 1 or cloud_minutes > 10_000_000
                ))):
            raise RuntimeError(
                "WHISPER_PAYSTACK_PLANS entries need billing_mode, positive amount_minor, uppercase "
                "currency, period_days (1-366), feature identifiers, and optional positive cloud_minutes; "
                "recurring plans also need code, "
                "while one_time plans may define Paystack channels"
            )
        plans[plan_id] = PaystackPlan(
            plan_id=plan_id,
            paystack_code=code or "",
            amount_minor=details["amount_minor"],
            currency=details["currency"],
            features=tuple(features),
            billing_mode=billing_mode,
            period_days=period_days,
            channels=tuple(channels),
            cloud_minutes=cloud_minutes,
        )
    return plans


if __name__ == "__main__":
    main()
