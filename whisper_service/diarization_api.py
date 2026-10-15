"""Authenticated HTTP transport for queued, self-hosted diarization."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import os
from pathlib import Path
import re
from urllib.parse import quote, urlparse
import wave
from typing import Any

from .auth import TokenAuthenticator
from .diarization_jobs import DiarizationJobManager, create_audio_tempfile
from .entitlements import EntitlementConfigError, EntitlementStore
from .sessions import ServiceError
from .billing import BillingStore, BillingStoreError
from .paystack import (
    PaystackPlan, PaystackWebhookError, apply_paystack_subscription_event,
    validate_checkout_request, verified_transaction_matches, verify_paystack_webhook,
)
from .accounts import AccountStore, AccountStoreError
from .analysis import AnalysisError, AnalysisModel, parse_result
from .oauth import OAuthTransactionError, OAuthTransactionStore, authorization_url
from .oauth_flow import OAuthProviderConfig, exchange_and_verify
from .oidc import OIDCVerificationError
from .plans import KIND_FEATURE
from .rewrite import RewriteError, RewriteModel


logger = logging.getLogger("whisper_service.diarization")
JOBS_PATH = "/v1/diarization/jobs"
PAYSTACK_WEBHOOK_PATH = "/v1/billing/paystack/webhook"
PAYSTACK_CHECKOUT_PATH = "/v1/billing/checkout"
PAYSTACK_STATUS_PATH = "/v1/billing/status"
PAYSTACK_MANAGE_PATH = "/v1/billing/manage"
PAYSTACK_PLANS_PATH = "/v1/billing/plans"
OAUTH_START_PATH = "/v1/auth/oauth/{provider}/start"
OAUTH_CALLBACK_PATH = "/v1/auth/oauth/{provider}/callback"
OAUTH_EXCHANGE_PATH = "/v1/auth/session/exchange"
REWRITE_PATH = "/v1/rewrite"
REWRITE_MODELS_PATH = "/v1/rewrite/models"
ANALYSIS_PATH = "/v1/analysis"
ANALYSIS_KINDS_PATH = "/v1/analysis/kinds"
MAX_ANALYSIS_CHARS = 60_000
MAX_UPLOAD_BYTES = 1_000_000_000
MAX_AUDIO_SECONDS = 4 * 60 * 60
UPLOAD_CHUNK_BYTES = 1024 * 1024


def make_app(authenticator: TokenAuthenticator, jobs: DiarizationJobManager | None,
             entitlements: EntitlementStore | None, *,
             billing_store: BillingStore | None = None,
             paystack_secret: str | None = None,
             paystack_plans: dict[str, PaystackPlan] | None = None,
             oauth_transactions: OAuthTransactionStore | None = None,
             account_store: AccountStore | None = None,
             oauth_providers: dict[str, OAuthProviderConfig] | None = None,
             rewrite_model: RewriteModel | None = None,
             analysis_model: AnalysisModel | None = None,
             analysis_cost_ms: int = 60_000,
             usage_plans: Any = None,
             oauth_app_redirect: str = "wicx://auth/callback") -> Any:
    try:
        from aiohttp import web
    except ImportError as exc:
        raise RuntimeError("Install universal-transcriber[server] to enable the diarization HTTP API") from exc

    @web.middleware
    async def authenticate(request: Any, handler: Any) -> Any:
        if request.path == PAYSTACK_WEBHOOK_PATH and request.method == "POST":
            return await handler(request)
        if oauth_transactions is not None and (
                request.path.startswith("/v1/auth/oauth/") or request.path == OAUTH_EXCHANGE_PATH):
            return await handler(request)
        user_id = authenticator.authenticate(request.headers.get("Authorization"))
        if user_id is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        request["user_id"] = user_id
        return await handler(request)

    async def create_job(request: Any) -> Any:
        if jobs is None or entitlements is None:
            return web.json_response({"error": "not_found"}, status=404)
        try:
            entitled = entitlements.has_feature(request["user_id"], "diarization")
        except EntitlementConfigError as exc:
            logger.error("Diarization entitlement check unavailable (%s)", type(exc).__name__)
            return web.json_response({"error": "entitlement_unavailable"}, status=503)
        if not entitled:
            return web.json_response({
                "error": "subscription_required",
                "message": "An active subscription is required for speaker analysis.",
            }, status=402)
        if request.content_type != "audio/wav":
            return web.json_response({"error": "content_type", "message": "Upload a WAV recording."}, status=415)
        if request.content_length is not None and request.content_length > MAX_UPLOAD_BYTES:
            return web.json_response({"error": "upload_too_large"}, status=413)
        fd, audio_path = create_audio_tempfile(jobs.temp_dir)
        size = 0
        try:
            with os.fdopen(fd, "wb") as output:
                async for chunk in request.content.iter_chunked(UPLOAD_CHUNK_BYTES):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        return web.json_response({"error": "upload_too_large"}, status=413)
                    output.write(chunk)
            _validate_wav(audio_path)
            job_id = await jobs.submit(request["user_id"], audio_path)
            return web.json_response({"job_id": job_id, "state": "queued"}, status=202)
        except ServiceError as exc:
            return web.json_response({"error": exc.code, "message": exc.message},
                                     status=503 if exc.retryable else 400)
        except ValueError as exc:
            return web.json_response({"error": "invalid_audio", "message": str(exc)}, status=400)
        except Exception as exc:
            logger.warning("Diarization upload failed (%s)", type(exc).__name__)
            return web.json_response({"error": "upload_failed"}, status=400)
        finally:
            # Successful submissions transfer ownership to the worker queue.
            if "job_id" not in locals():
                audio_path.unlink(missing_ok=True)

    async def rewrite_text(request: Any) -> Any:
        if rewrite_model is None or entitlements is None:
            return web.json_response({"error": "rewrite_unavailable"}, status=503)
        try:
            entitled = entitlements.has_feature(request["user_id"], "cloud_rewrite")
        except EntitlementConfigError as exc:
            logger.error("Rewrite entitlement check unavailable (%s)", type(exc).__name__)
            return web.json_response({"error": "entitlement_unavailable"}, status=503)
        if not entitled:
            return web.json_response({
                "error": "subscription_required",
                "message": "An active subscription is required for cloud rewriting.",
            }, status=402)
        if request.content_type != "application/json":
            return web.json_response({"error": "content_type"}, status=415)
        if request.content_length is not None and request.content_length > 65_536:
            return web.json_response({"error": "request_too_large"}, status=413)
        try:
            raw = await request.content.read(65_537)
            if len(raw) > 65_536:
                return web.json_response({"error": "request_too_large"}, status=413)
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return web.json_response({"error": "invalid_request"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "invalid_request"}, status=400)
        text = body.get("text")
        model = body.get("model")
        if (not isinstance(text, str) or not text.strip() or len(text) > 20_000 or
                (model is not None and (not isinstance(model, str) or len(model) > 160))):
            return web.json_response({"error": "invalid_request"}, status=400)
        if model is not None and model not in rewrite_model.models:
            return web.json_response({"error": "unsupported_model"}, status=400)
        try:
            rewritten, selected_model = await rewrite_model.rewrite(text.strip(), model=model)
        except RewriteError as exc:
            logger.warning("Cloud rewrite failed (%s)", str(exc))
            return web.json_response({"error": "rewrite_unavailable"}, status=503)
        return web.json_response({"text": rewritten, "model": selected_model})

    async def analyze_text(request: Any) -> Any:
        if analysis_model is None or entitlements is None:
            return web.json_response({"error": "analysis_unavailable"}, status=503)
        if request.content_type != "application/json":
            return web.json_response({"error": "content_type"}, status=415)
        try:
            raw = await request.content.read(MAX_ANALYSIS_CHARS + 1)
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return web.json_response({"error": "invalid_request"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "invalid_request"}, status=400)
        kind = body.get("kind")
        text = body.get("text")
        language = body.get("language")
        model = body.get("model")
        if (not isinstance(kind, str) or kind not in KIND_FEATURE
                or not isinstance(text, str) or not text.strip() or len(text) > MAX_ANALYSIS_CHARS
                or (language is not None and (not isinstance(language, str) or len(language) > 32))
                or (model is not None and (not isinstance(model, str) or len(model) > 160))):
            return web.json_response({"error": "invalid_request"}, status=400)
        try:
            entitled = entitlements.has_feature(request["user_id"], KIND_FEATURE[kind])
        except EntitlementConfigError as exc:
            logger.error("Analysis entitlement check unavailable (%s)", type(exc).__name__)
            return web.json_response({"error": "entitlement_unavailable"}, status=503)
        if not entitled:
            return web.json_response({
                "error": "subscription_required",
                "message": "An active subscription is required for session analysis.",
            }, status=402)
        budget_plan = None
        if billing_store is not None and usage_plans:
            try:
                budget = await asyncio.to_thread(
                    billing_store.cloud_usage_budget, request["user_id"], usage_plans,
                )
            except BillingStoreError as exc:
                logger.error("Analysis budget check unavailable (%s)", type(exc).__name__)
                return web.json_response({"error": "entitlement_unavailable"}, status=503)
            if budget is None or (budget["remaining_milliseconds"] is not None
                                  and int(budget["remaining_milliseconds"]) < analysis_cost_ms):
                return web.json_response({
                    "error": "quota_exhausted",
                    "message": "The subscription allowance is exhausted. Analysis resumes after renewal or upgrade.",
                }, status=402)
            budget_plan = budget
        if model is not None and model not in analysis_model.models:
            return web.json_response({"error": "unsupported_model"}, status=400)
        try:
            content, selected_model = await analysis_model.analyze(
                kind, text.strip(), language=language, model=model,
            )
            result = parse_result(kind, content)
        except AnalysisError as exc:
            logger.warning("Cloud analysis failed (%s)", str(exc))
            return web.json_response({"error": "analysis_unavailable"}, status=503)
        if budget_plan is not None and billing_store is not None:
            try:
                await asyncio.to_thread(
                    billing_store.record_cloud_usage, request["user_id"],
                    str(budget_plan["plan_id"]), str(budget_plan["period_end"]), analysis_cost_ms,
                )
            except BillingStoreError as exc:
                logger.error("Analysis usage record unavailable (%s)", type(exc).__name__)
        return web.json_response({"kind": kind, "result": result, "model": selected_model})

    async def analysis_kinds(request: Any) -> Any:
        if analysis_model is None or entitlements is None:
            return web.json_response({"error": "analysis_unavailable"}, status=503)
        kinds = []
        for kind, feature in KIND_FEATURE.items():
            try:
                entitled = entitlements.has_feature(request["user_id"], feature)
            except EntitlementConfigError as exc:
                logger.error("Analysis entitlement check unavailable (%s)", type(exc).__name__)
                return web.json_response({"error": "entitlement_unavailable"}, status=503)
            if entitled:
                kinds.append({"kind": kind, "models": list(analysis_model.models)})
        return web.json_response({"kinds": kinds, "default_model": analysis_model.models[0]})

    async def rewrite_models(request: Any) -> Any:
        if rewrite_model is None or entitlements is None:
            return web.json_response({"error": "rewrite_unavailable"}, status=503)
        try:
            entitled = entitlements.has_feature(request["user_id"], "cloud_rewrite")
        except EntitlementConfigError as exc:
            logger.error("Rewrite entitlement check unavailable (%s)", type(exc).__name__)
            return web.json_response({"error": "entitlement_unavailable"}, status=503)
        if not entitled:
            return web.json_response({"error": "subscription_required"}, status=402)
        return web.json_response({"models": list(rewrite_model.models), "default_model": rewrite_model.models[0]})

    async def get_job(request: Any) -> Any:
        if jobs is None:
            return web.json_response({"error": "not_found"}, status=404)
        job_id = request.match_info["job_id"]
        if len(job_id) != 32 or any(ch not in "0123456789abcdef" for ch in job_id):
            return web.json_response({"error": "not_found"}, status=404)
        result = await jobs.get(request["user_id"], job_id)
        if result is None:
            # Do not disclose whether another user's job ID exists.
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response(result)

    async def paystack_webhook(request: Any) -> Any:
        if billing_store is None or not paystack_secret:
            return web.json_response({"error": "billing_unavailable"}, status=503)
        if request.content_length is not None and request.content_length > 1_000_000:
            return web.json_response({"error": "webhook_too_large"}, status=413)
        body = await request.content.read(1_000_001)
        if len(body) > 1_000_000:
            return web.json_response({"error": "webhook_too_large"}, status=413)
        try:
            event = verify_paystack_webhook(
                body, request.headers.get("x-paystack-signature"), paystack_secret,
            )
        except PaystackWebhookError as exc:
            return web.json_response({"error": "invalid_webhook", "message": str(exc)}, status=401)
        if event.name == "charge.success":
            reference = event.data.get("reference")
            if not isinstance(reference, str) or not reference:
                return web.json_response({"received": True, "applied": False})
            subscription = event.data.get("subscription")
            subscription_code = subscription.get("subscription_code") if isinstance(subscription, dict) else None
            try:
                checkout = billing_store.get_checkout(reference)
                current = (billing_store.get_subscription("paystack", subscription_code)
                           if isinstance(subscription_code, str) else None)
            except BillingStoreError as exc:
                logger.error("Could not read Paystack mapping (%s)", type(exc).__name__)
                return web.json_response({"error": "billing_unavailable"}, status=503)
            if checkout is None and current is None:
                # Do not query Paystack or create an account mapping for an
                # uncorrelated charge notification.
                return web.json_response({"received": True, "applied": False})
            expected_plan_id = checkout["plan_id"] if checkout else current["plan_id"]
            plan = paystack_plans.get(str(expected_plan_id)) if paystack_plans else None
            expected_amount = checkout["amount_minor"] if checkout else (plan.amount_minor if plan else None)
            expected_currency = checkout["currency"] if checkout else (plan.currency if plan else None)
            if (plan is None or not isinstance(expected_amount, int) or
                    expected_amount != plan.amount_minor or expected_currency != plan.currency):
                return web.json_response({"received": True, "applied": False})
            if checkout is not None:
                # The durable checkout mapping, not webhook-supplied metadata,
                # is authoritative for the first account-to-subscription link.
                verified_data = dict(event.data)
                verified_data["metadata"] = {
                    "wicx_user_id": checkout["user_id"],
                    "wicx_plan_id": checkout["plan_id"],
                }
                if checkout["billing_mode"] == "one_time":
                    verified_data["_wicx_manual_checkout"] = {
                        "reference": checkout["reference"],
                        "plan_id": checkout["plan_id"],
                        "period_days": checkout["period_days"],
                    }
                event = type(event)(
                    name=event.name, data=verified_data, digest=event.digest,
                    occurred_at=event.occurred_at,
                )
            try:
                from aiohttp import ClientSession, ClientTimeout

                timeout = ClientTimeout(total=15)
                async with ClientSession(timeout=timeout) as session:
                    async with session.get(
                        f"https://api.paystack.co/transaction/verify/{quote(reference, safe='')}",
                        headers={"Authorization": f"Bearer {paystack_secret}"},
                        allow_redirects=False,
                    ) as response:
                        provider_result = await response.json(content_type=None)
                        if response.status != 200:
                            return web.json_response({"error": "verification_unavailable"}, status=503)
            except Exception as exc:
                logger.warning("Paystack transaction verification failed (%s)", type(exc).__name__)
                return web.json_response({"error": "verification_unavailable"}, status=503)
            if not verified_transaction_matches(
                provider_result, reference=reference,
                amount_minor=expected_amount, currency=str(expected_currency),
            ):
                return web.json_response({"received": True, "applied": False})
            webhook_amount = event.data.get("amount")
            if (webhook_amount != expected_amount or isinstance(webhook_amount, bool) or
                    event.data.get("currency") != expected_currency):
                return web.json_response({"received": True, "applied": False})
        try:
            applied = apply_paystack_subscription_event(billing_store, event, paystack_plans)
        except PaystackWebhookError as exc:
            logger.error("Paystack event persistence failed (%s)", type(exc).__name__)
            return web.json_response({"error": "billing_unavailable"}, status=503)
        return web.json_response({"received": True, "applied": applied})

    async def create_checkout(request: Any) -> Any:
        if billing_store is None or not paystack_secret or not paystack_plans:
            return web.json_response({"error": "billing_unavailable"}, status=503)
        if request.content_type != "application/json":
            return web.json_response({"error": "content_type"}, status=415)
        if request.content_length is not None and request.content_length > 16_384:
            return web.json_response({"error": "request_too_large"}, status=413)
        try:
            raw_body = await request.content.read(16_385)
            if len(raw_body) > 16_384:
                return web.json_response({"error": "request_too_large"}, status=413)
            body = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError):
            return web.json_response({"error": "invalid_request"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "invalid_request"}, status=400)
        try:
            _provider_plan, fields = validate_checkout_request(
                user_id=request["user_id"], email=body.get("email"),
                plan_id=body.get("plan_id"), plans=paystack_plans,
            )
        except PaystackWebhookError as exc:
            return web.json_response({"error": "invalid_checkout", "message": str(exc)}, status=400)

        # The provider plan code, amount/channel, and account metadata are
        # entirely server-owned; no billing values come from the request.
        try:
            from aiohttp import ClientSession, ClientTimeout

            timeout = ClientTimeout(total=20)
            async with ClientSession(timeout=timeout) as session:
                async with session.post(
                    "https://api.paystack.co/transaction/initialize",
                    json=fields,
                    headers={"Authorization": f"Bearer {paystack_secret}"},
                    allow_redirects=False,
                ) as response:
                    result = await response.json(content_type=None)
                    if response.status != 200 or not isinstance(result, dict) or result.get("status") is not True:
                        logger.warning("Paystack checkout initialization failed (HTTP %s)", response.status)
                        return web.json_response({"error": "checkout_unavailable"}, status=502)
        except Exception as exc:
            logger.warning("Paystack checkout request failed (%s)", type(exc).__name__)
            return web.json_response({"error": "checkout_unavailable"}, status=502)
        data = result.get("data")
        if not isinstance(data, dict):
            return web.json_response({"error": "checkout_unavailable"}, status=502)
        checkout_url = data.get("authorization_url")
        reference = data.get("reference")
        parsed_url = urlparse(checkout_url) if isinstance(checkout_url, str) else None
        if (parsed_url is None or parsed_url.scheme != "https" or
                parsed_url.hostname != "checkout.paystack.com" or
                not isinstance(reference, str) or not re.fullmatch(r"[A-Za-z0-9.=-]{1,100}", reference)):
            logger.warning("Paystack returned an invalid checkout response")
            return web.json_response({"error": "checkout_unavailable"}, status=502)
        selected_plan = paystack_plans[body["plan_id"]]
        try:
            billing_store.record_checkout(
                reference=reference,
                user_id=request["user_id"],
                plan_id=selected_plan.plan_id,
                amount_minor=selected_plan.amount_minor,
                currency=selected_plan.currency,
                billing_mode=selected_plan.billing_mode,
                period_days=selected_plan.period_days,
            )
        except (ValueError, BillingStoreError) as exc:
            logger.error("Could not persist Paystack checkout mapping (%s)", type(exc).__name__)
            return web.json_response({"error": "billing_unavailable"}, status=503)
        return web.json_response({"authorization_url": checkout_url, "reference": reference})

    async def billing_status(request: Any) -> Any:
        if billing_store is None:
            return web.json_response({"error": "not_found"}, status=404)
        try:
            subscriptions = billing_store.list_user_subscriptions(request["user_id"])
            feature_ids = sorted({
                feature for plan in (paystack_plans or {}).values()
                for feature in plan.features
            })
            features = {
                feature: bool(entitlements and entitlements.has_feature(request["user_id"], feature))
                for feature in feature_ids
            }
            usage = billing_store.cloud_usage_budget(
                request["user_id"],
                {key: (plan.features, plan.cloud_minutes) for key, plan in (paystack_plans or {}).items()},
            )
        except (BillingStoreError, EntitlementConfigError) as exc:
            logger.error("Could not resolve billing status (%s)", type(exc).__name__)
            return web.json_response({"error": "billing_unavailable"}, status=503)
        return web.json_response({
            "features": features,
            "subscriptions": subscriptions,
            "usage": {"cloud_transcription": usage} if usage is not None else {},
        })

    async def billing_plans(_request: Any) -> Any:
        if not paystack_plans:
            return web.json_response({"error": "not_found"}, status=404)
        plans = []
        for plan in sorted(paystack_plans.values(), key=lambda item: item.plan_id):
            entry: dict[str, Any] = {
                "plan_id": plan.plan_id,
                "amount_minor": plan.amount_minor,
                "currency": plan.currency,
                "billing_mode": plan.billing_mode,
                "features": list(plan.features),
            }
            if plan.cloud_minutes is not None:
                entry["cloud_minutes"] = plan.cloud_minutes
            if plan.billing_mode == "one_time":
                entry.update({
                    "period_days": plan.period_days,
                    "channels": list(plan.channels),
                    "renewal": "manual",
                })
            else:
                entry["renewal"] = "automatic"
            plans.append(entry)
        return web.json_response({"plans": plans})

    async def manage_subscription(request: Any) -> Any:
        if billing_store is None or not paystack_secret or not paystack_plans:
            return web.json_response({"error": "billing_unavailable"}, status=503)
        if request.content_type != "application/json":
            return web.json_response({"error": "content_type"}, status=415)
        try:
            raw_body = await request.content.read(16_385)
            if len(raw_body) > 16_384:
                return web.json_response({"error": "request_too_large"}, status=413)
            body = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError):
            return web.json_response({"error": "invalid_request"}, status=400)
        plan_id = body.get("plan_id") if isinstance(body, dict) else None
        if not isinstance(plan_id, str) or plan_id not in paystack_plans:
            return web.json_response({"error": "invalid_plan"}, status=400)
        plan = paystack_plans[plan_id]
        if plan.billing_mode != "recurring":
            return web.json_response({"error": "manual_renewal_plan"}, status=409)
        try:
            subscription_code = billing_store.get_manageable_subscription(
                request["user_id"], plan_id,
            )
        except (BillingStoreError, ValueError) as exc:
            logger.error("Could not resolve subscription management (%s)", type(exc).__name__)
            return web.json_response({"error": "billing_unavailable"}, status=503)
        if subscription_code is None:
            return web.json_response({"error": "active_subscription_not_found"}, status=404)
        try:
            from aiohttp import ClientSession, ClientTimeout

            timeout = ClientTimeout(total=15)
            async with ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"https://api.paystack.co/subscription/{quote(subscription_code, safe='')}/manage/link",
                    headers={"Authorization": f"Bearer {paystack_secret}"},
                    allow_redirects=False,
                ) as response:
                    result = await response.json(content_type=None)
                    if response.status != 200 or not isinstance(result, dict) or result.get("status") is not True:
                        logger.warning("Paystack subscription management link failed (HTTP %s)", response.status)
                        return web.json_response({"error": "subscription_management_unavailable"}, status=502)
        except Exception as exc:
            logger.warning("Paystack subscription management request failed (%s)", type(exc).__name__)
            return web.json_response({"error": "subscription_management_unavailable"}, status=502)
        data = result.get("data")
        manage_url = data.get("link") if isinstance(data, dict) else None
        parsed_url = urlparse(manage_url) if isinstance(manage_url, str) else None
        if (parsed_url is None or parsed_url.scheme != "https" or
                parsed_url.hostname != "paystack.com" or
                not parsed_url.path.startswith("/manage/subscriptions/") or
                not parsed_url.query):
            logger.warning("Paystack returned an invalid subscription management link")
            return web.json_response({"error": "subscription_management_unavailable"}, status=502)
        return web.json_response({"management_url": manage_url})

    async def oauth_start(request: Any) -> Any:
        if oauth_transactions is None:
            return web.json_response({"error": "sign_in_unavailable"}, status=503)
        provider = request.match_info["provider"]
        config = (oauth_providers or {}).get(provider)
        if config is None:
            return web.json_response({"error": "provider_unavailable"}, status=503)
        try:
            data = await request.json()
            challenge = data.get("code_challenge") if isinstance(data, dict) else None
            transaction = oauth_transactions.begin(provider=provider, pkce_challenge=challenge)
            target = authorization_url(
                provider=provider, client_id=config.client_id,
                redirect_uri=config.redirect_uri, transaction=transaction,
            )
        except (ValueError, OAuthTransactionError):
            return web.json_response({"error": "invalid_request"}, status=400)
        return web.json_response({"authorization_url": target})

    async def oauth_callback(request: Any) -> Any:
        if oauth_transactions is None or account_store is None:
            return web.json_response({"error": "sign_in_unavailable"}, status=503)
        provider = request.match_info["provider"]
        config = (oauth_providers or {}).get(provider)
        if config is None:
            return web.json_response({"error": "provider_unavailable"}, status=503)
        try:
            values = await request.post() if request.method == "POST" else request.query
            state, code = values.get("state", ""), values.get("code", "")
            if values.get("error") or not code:
                return web.Response(text="Wicx sign-in was cancelled or could not be completed.", status=400)
            pending = oauth_transactions.pending_callback(provider=provider, state=state)
            from aiohttp import ClientSession, ClientTimeout

            async with ClientSession(timeout=ClientTimeout(total=15)) as session:
                identity = await exchange_and_verify(
                    config=config, code=code,
                    provider_pkce_verifier=pending.provider_pkce_verifier,
                    nonce=pending.nonce, session=session,
                )
            account = account_store.resolve_verified_identity(
                provider=identity.provider, subject=identity.subject,
                email=identity.email, email_verified=identity.email_verified,
            )
            handoff = oauth_transactions.complete_callback(
                provider=provider, state=state, verified_nonce=identity.nonce,
                account_id=account.account_id,
            )
            from urllib.parse import urlencode

            return web.Response(
                status=302,
                headers={"Location": f"{oauth_app_redirect}?{urlencode({'code': handoff})}"},
            )
        except (OAuthTransactionError, OIDCVerificationError, AccountStoreError, ValueError) as exc:
            logger.info("OAuth callback rejected (%s)", type(exc).__name__)
            return web.Response(
                text="Wicx could not verify this sign-in. Return to the app and try again.", status=400,
            )
        except Exception as exc:
            logger.warning("OAuth callback failed (%s)", type(exc).__name__)
            return web.Response(text="Wicx sign-in is temporarily unavailable.", status=502)

    async def exchange_oauth_session(request: Any) -> Any:
        if oauth_transactions is None or account_store is None:
            return web.json_response({"error": "sign_in_unavailable"}, status=503)
        try:
            data = await request.json()
            handoff = data.get("code") if isinstance(data, dict) else None
            verifier = data.get("code_verifier") if isinstance(data, dict) else None
            account_id = oauth_transactions.redeem_handoff(code=handoff, pkce_verifier=verifier)
            token = account_store.issue_session(account_id)
        except (ValueError, OAuthTransactionError):
            return web.json_response({"error": "invalid_or_expired_handoff"}, status=400)
        except AccountStoreError:
            logger.exception("Could not create OAuth account session")
            return web.json_response({"error": "session_unavailable"}, status=503)
        return web.json_response({"access_token": token, "token_type": "Bearer", "expires_in": 2592000})

    app = web.Application(middlewares=[authenticate], client_max_size=MAX_UPLOAD_BYTES)
    if jobs is not None:
        app.router.add_post(JOBS_PATH, create_job)
        app.router.add_get(f"{JOBS_PATH}/{{job_id}}", get_job)
    if rewrite_model is not None:
        app.router.add_post(REWRITE_PATH, rewrite_text)
        app.router.add_get(REWRITE_MODELS_PATH, rewrite_models)
    if analysis_model is not None:
        app.router.add_post(ANALYSIS_PATH, analyze_text)
        app.router.add_get(ANALYSIS_KINDS_PATH, analysis_kinds)
    if billing_store is not None:
        app.router.add_post(PAYSTACK_WEBHOOK_PATH, paystack_webhook)
        app.router.add_get(PAYSTACK_STATUS_PATH, billing_status)
        app.router.add_get(PAYSTACK_PLANS_PATH, billing_plans)
    if paystack_secret and paystack_plans:
        app.router.add_post(PAYSTACK_CHECKOUT_PATH, create_checkout)
        app.router.add_post(PAYSTACK_MANAGE_PATH, manage_subscription)
    if oauth_transactions is not None:
        app.router.add_post(OAUTH_START_PATH, oauth_start)
        app.router.add_get(OAUTH_CALLBACK_PATH, oauth_callback)
        app.router.add_post(OAUTH_CALLBACK_PATH, oauth_callback)
        app.router.add_post(OAUTH_EXCHANGE_PATH, exchange_oauth_session)
    return app


def _validate_wav(path: Path) -> None:
    try:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frame_count = source.getnframes()
            compression = source.getcomptype()
    except (wave.Error, EOFError) as exc:
        raise ValueError("The uploaded file is not a valid WAV recording.") from exc
    if channels != 1 or sample_width != 2 or sample_rate != 16_000 or compression != "NONE":
        raise ValueError("WAV must be uncompressed mono PCM16 at 16 kHz.")
    if frame_count <= 0 or frame_count / sample_rate > MAX_AUDIO_SECONDS:
        raise ValueError("Recording must be longer than zero and no longer than four hours.")


async def serve_http(authenticator: TokenAuthenticator, jobs: DiarizationJobManager | None, *,
                     entitlements: EntitlementStore | None,
                     billing_store: BillingStore | None = None,
                     paystack_secret: str | None = None,
                     paystack_plans: dict[str, PaystackPlan] | None = None,
                     oauth_transactions: OAuthTransactionStore | None = None,
                     account_store: AccountStore | None = None,
                     oauth_providers: dict[str, OAuthProviderConfig] | None = None,
                     rewrite_model: RewriteModel | None = None,
                     analysis_model: AnalysisModel | None = None,
                     analysis_cost_ms: int = 60_000,
                     usage_plans: Any = None,
                     oauth_app_redirect: str = "wicx://auth/callback",
                     host: str = "127.0.0.1", port: int = 8766) -> None:
    try:
        from aiohttp import web
    except ImportError as exc:
        raise RuntimeError("Install universal-transcriber[server] to enable the diarization HTTP API") from exc
    if jobs is not None:
        await jobs.start()
    runner = web.AppRunner(make_app(
        authenticator, jobs, entitlements,
        billing_store=billing_store, paystack_secret=paystack_secret,
        paystack_plans=paystack_plans,
        oauth_transactions=oauth_transactions, account_store=account_store,
        oauth_providers=oauth_providers, oauth_app_redirect=oauth_app_redirect,
        rewrite_model=rewrite_model,
        analysis_model=analysis_model,
        analysis_cost_ms=analysis_cost_ms,
        usage_plans=usage_plans,
    ), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    try:
        await site.start()
        logger.info("Diarization HTTP API listening on %s:%d", host, port)
        await asyncio.Future()
    finally:
        await runner.cleanup()
        if jobs is not None:
            await jobs.close()
