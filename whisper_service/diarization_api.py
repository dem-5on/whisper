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


logger = logging.getLogger("whisper_service.diarization")
JOBS_PATH = "/v1/diarization/jobs"
PAYSTACK_WEBHOOK_PATH = "/v1/billing/paystack/webhook"
PAYSTACK_CHECKOUT_PATH = "/v1/billing/checkout"
MAX_UPLOAD_BYTES = 1_000_000_000
MAX_AUDIO_SECONDS = 4 * 60 * 60
UPLOAD_CHUNK_BYTES = 1024 * 1024


def make_app(authenticator: TokenAuthenticator, jobs: DiarizationJobManager | None,
             entitlements: EntitlementStore | None, *,
             billing_store: BillingStore | None = None,
             paystack_secret: str | None = None,
             paystack_plans: dict[str, PaystackPlan] | None = None) -> Any:
    try:
        from aiohttp import web
    except ImportError as exc:
        raise RuntimeError("Install universal-transcriber[server] to enable the diarization HTTP API") from exc

    @web.middleware
    async def authenticate(request: Any, handler: Any) -> Any:
        if request.path == PAYSTACK_WEBHOOK_PATH and request.method == "POST":
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
            provider_plan, fields = validate_checkout_request(
                user_id=request["user_id"], email=body.get("email"),
                plan_id=body.get("plan_id"), plans=paystack_plans,
            )
        except PaystackWebhookError as exc:
            return web.json_response({"error": "invalid_checkout", "message": str(exc)}, status=400)

        # The provider plan code and account metadata are entirely server-owned.
        # No amount or user ID from the client is sent to Paystack.
        fields["plan"] = provider_plan
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
            )
        except (ValueError, BillingStoreError) as exc:
            logger.error("Could not persist Paystack checkout mapping (%s)", type(exc).__name__)
            return web.json_response({"error": "billing_unavailable"}, status=503)
        return web.json_response({"authorization_url": checkout_url, "reference": reference})

    app = web.Application(middlewares=[authenticate], client_max_size=MAX_UPLOAD_BYTES)
    if jobs is not None:
        app.router.add_post(JOBS_PATH, create_job)
        app.router.add_get(f"{JOBS_PATH}/{{job_id}}", get_job)
    if billing_store is not None:
        app.router.add_post(PAYSTACK_WEBHOOK_PATH, paystack_webhook)
    if paystack_secret and paystack_plans:
        app.router.add_post(PAYSTACK_CHECKOUT_PATH, create_checkout)
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
