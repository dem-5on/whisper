"""WebSocket transport for the hosted live-transcription service."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
import json
import logging
from typing import Any

from .auth import TokenAuthenticator
from .protocol import encode_event, parse_control, parse_session_start
from .sessions import ServiceError, SessionManager


logger = logging.getLogger("whisper_service")
LIVE_PATH = "/v1/live"
MAX_WEBSOCKET_MESSAGE_BYTES = 64 * 1024


def make_process_request(authenticator: TokenAuthenticator) -> Any:
    """Reject wrong paths and unauthenticated clients before WS upgrade."""
    async def process_request(connection: Any, request: Any) -> Any:
        if request.path == "/healthz" and request.method == "GET":
            return connection.respond(HTTPStatus.OK, "ok\n")
        if request.path != LIVE_PATH:
            return connection.respond(HTTPStatus.NOT_FOUND, "Not found\n")
        user_id = authenticator.authenticate(request.headers.get("Authorization"))
        if user_id is None:
            return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")
        return None
    return process_request


def make_handler(authenticator: TokenAuthenticator, sessions: SessionManager) -> Any:
    async def handler(connection: Any) -> None:
        user_id = authenticator.authenticate(connection.request.headers.get("Authorization"))
        if user_id is None:  # Defense in depth if handler use changes later.
            await connection.close(code=4401, reason="Unauthorized")
            return

        session = None
        try:
            first = await connection.recv()
            if not isinstance(first, str):
                raise ServiceError("invalid_request", "The first message must be a JSON session.start message.")
            profile_id, language = parse_session_start(first)
            session = await sessions.start(user_id, profile_id, language)
            await connection.send(encode_event({
                "type": "session.ready",
                "session_id": session.session_id,
                "audio": {"encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1},
                "limits": {
                    "max_audio_seconds": sessions.limits.max_audio_seconds,
                    "max_chunk_bytes": sessions.limits.max_chunk_bytes,
                },
            }))

            async for message in connection:
                if isinstance(message, bytes):
                    partial = await session.submit_audio(message)
                    if partial is not None:
                        await connection.send(encode_event(partial))
                    continue
                control = parse_control(message)
                if control == "session.finish":
                    await connection.send(encode_event(await session.finish()))
                    await connection.send(encode_event({"type": "session.closed"}))
                    session = None
                    return
                await session.cancel()
                await connection.send(encode_event({"type": "session.closed"}))
                session = None
                return
        except ServiceError as exc:
            await _send_error(connection, exc)
        except Exception as exc:
            # Avoid logging exception text: inference/library failures may
            # contain paths or other deployment details.
            logger.warning("Live connection failed (%s)", type(exc).__name__)
            await _send_error(connection, ServiceError("internal_error", "The live session failed.", retryable=True))
        finally:
            if session is not None:
                await session.disconnect()

    return handler


async def _send_error(connection: Any, error: ServiceError) -> None:
    try:
        await connection.send(encode_event({
            "type": "session.error", "code": error.code,
            "retryable": error.retryable, "message": error.message,
        }))
        await connection.close(code=1011 if error.retryable else 1008, reason=error.code)
    except Exception:
        pass  # A failed socket cannot receive its error event.


async def serve_websocket(authenticator: TokenAuthenticator, sessions: SessionManager, *,
                          host: str = "127.0.0.1", port: int = 8765) -> None:
    """Run the live endpoint; deploy behind a TLS-terminating reverse proxy."""
    try:
        from websockets.asyncio.server import serve
    except ImportError as exc:
        raise RuntimeError("Install universal-transcriber[server] to run the hosted service") from exc

    async with serve(
        make_handler(authenticator, sessions), host, port,
        process_request=make_process_request(authenticator),
        max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
        max_queue=8,
        compression=None,
        ping_interval=20,
        ping_timeout=20,
        server_header=None,
    ):
        logger.info("Whisper live service listening on %s:%d", host, port)
        await asyncio.Future()
