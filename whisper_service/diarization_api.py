"""Authenticated HTTP transport for queued, self-hosted diarization."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import os
from pathlib import Path
import wave
from typing import Any

from .auth import TokenAuthenticator
from .diarization_jobs import DiarizationJobManager, create_audio_tempfile
from .sessions import ServiceError


logger = logging.getLogger("whisper_service.diarization")
JOBS_PATH = "/v1/diarization/jobs"
MAX_UPLOAD_BYTES = 1_000_000_000
MAX_AUDIO_SECONDS = 4 * 60 * 60
UPLOAD_CHUNK_BYTES = 1024 * 1024


def make_app(authenticator: TokenAuthenticator, jobs: DiarizationJobManager) -> Any:
    try:
        from aiohttp import web
    except ImportError as exc:
        raise RuntimeError("Install universal-transcriber[server] to enable the diarization HTTP API") from exc

    @web.middleware
    async def authenticate(request: Any, handler: Any) -> Any:
        user_id = authenticator.authenticate(request.headers.get("Authorization"))
        if user_id is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        request["user_id"] = user_id
        return await handler(request)

    async def create_job(request: Any) -> Any:
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
        job_id = request.match_info["job_id"]
        if len(job_id) != 32 or any(ch not in "0123456789abcdef" for ch in job_id):
            return web.json_response({"error": "not_found"}, status=404)
        result = await jobs.get(request["user_id"], job_id)
        if result is None:
            # Do not disclose whether another user's job ID exists.
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response(result)

    app = web.Application(middlewares=[authenticate], client_max_size=MAX_UPLOAD_BYTES)
    app.router.add_post(JOBS_PATH, create_job)
    app.router.add_get(f"{JOBS_PATH}/{{job_id}}", get_job)
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


async def serve_http(authenticator: TokenAuthenticator, jobs: DiarizationJobManager, *,
                     host: str = "127.0.0.1", port: int = 8766) -> None:
    try:
        from aiohttp import web
    except ImportError as exc:
        raise RuntimeError("Install universal-transcriber[server] to enable the diarization HTTP API") from exc
    await jobs.start()
    runner = web.AppRunner(make_app(authenticator, jobs), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    try:
        await site.start()
        logger.info("Diarization HTTP API listening on %s:%d", host, port)
        await asyncio.Future()
    finally:
        await runner.cleanup()
        await jobs.close()
