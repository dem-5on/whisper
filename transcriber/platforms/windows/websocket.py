"""Token-authenticated loopback WebSocket bridge for the Windows Tauri panel.

The daemon remains the source of truth: this module only translates the
existing command/status/event API to WebSocket messages for the webview.
"""

from __future__ import annotations

import asyncio
from http import HTTPStatus
import hmac
import json
import logging
import threading
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ...daemon import TranscriberDaemon

LOG = logging.getLogger("transcriber.windows.websocket")
HOST = "127.0.0.1"
PORT = 47651
MAX_MESSAGE_BYTES = 8192
ALLOWED_ORIGINS = (
    "http://tauri.localhost",
    "https://tauri.localhost",
    "http://127.0.0.1:1420",
    "http://localhost:1420",
)


def _request_id(request: dict[str, Any]) -> str | int | None:
    request_id = request.get("request_id")
    return request_id if isinstance(request_id, (str, int)) else None


def _error(message: str, request_id: str | int | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"ok": "false", "error": message}
    if request_id is not None:
        response["request_id"] = request_id
    return response


def _valid_ui_token(request_path: str, expected_token: str | None) -> bool:
    """Validate the per-launch token; empty tokens are reserved for dev mode."""
    if not expected_token:
        return True
    try:
        supplied = parse_qs(urlsplit(request_path).query, max_num_fields=8).get("token", [])
    except ValueError:
        return False
    return (
        len(supplied) == 1
        and supplied[0].isascii()
        and hmac.compare_digest(supplied[0], expected_token)
    )


def _authenticate_handshake(connection: Any, request: Any, expected_token: str | None) -> Any:
    """Reject unauthenticated clients before completing the WebSocket upgrade."""
    if _valid_ui_token(request.path, expected_token):
        return None
    return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")


async def handle_connection(daemon: TranscriberDaemon, websocket: Any) -> None:
    """Serve daemon commands and live events over the authenticated local connection."""
    send_lock = asyncio.Lock()
    subscription: asyncio.Task[None] | None = None

    async def send(payload: dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send(json.dumps(payload, separators=(",", ":")))

    async def publish_updates(since: int, session_id: str | None) -> None:
        last_revision = since
        previous_status = daemon.status_dict()
        try:
            await send({"type": "status", "status": previous_status})
            while True:
                events, revision = await asyncio.to_thread(
                    daemon.wait_events_since, last_revision, session_id, 0.5
                )
                for event in events:
                    await send({"type": "daemon_event", "event": event})
                    last_revision = max(last_revision, int(event.get("revision", last_revision)))
                status = daemon.status_dict()
                # Elapsed timers are rendered locally by JS; only push when
                # meaningful daemon-owned state has changed.
                for transient in ("recording_elapsed", "processing_elapsed"):
                    status.pop(transient, None)
                    previous_status.pop(transient, None)
                if status != previous_status:
                    await send({"type": "status", "status": status})
                    previous_status = status
                last_revision = max(last_revision, revision)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.debug("Desktop WebSocket update stream ended", exc_info=True)

    try:
        async for raw in websocket:
            request_id: str | int | None = None
            try:
                request = json.loads(raw)
                if not isinstance(request, dict) or not isinstance(request.get("command"), str):
                    raise ValueError
                request_id = _request_id(request)
            except (json.JSONDecodeError, ValueError, TypeError):
                await send(_error("Invalid request", request_id))
                continue

            command = request["command"]
            if command == "subscribe":
                try:
                    since = max(0, int(request.get("since", 0)))
                except (TypeError, ValueError):
                    since = 0
                session_id = request.get("session_id")
                if subscription is not None:
                    subscription.cancel()
                await send({"ok": "true", "subscribed": True, "request_id": request_id})
                subscription = asyncio.create_task(
                    publish_updates(since, str(session_id) if session_id else None)
                )
                continue

            if command == "shutdown":
                await send(_error("The UI cannot shut down the daemon", request_id))
                continue

            params = {
                key: value
                for key, value in request.items()
                if key not in {"command", "request_id"}
            }
            try:
                response = await asyncio.to_thread(daemon.command, command, params)
            except Exception:
                LOG.error("Desktop command failed: %s", command)
                response = _error("The daemon could not complete that command", request_id)
            else:
                response["request_id"] = request_id
            await send(response)
    finally:
        if subscription is not None:
            subscription.cancel()
            try:
                await subscription
            except asyncio.CancelledError:
                pass


async def _serve(
    daemon: TranscriberDaemon,
    host: str,
    port: int,
    auth_token: str | None,
    started: threading.Event,
) -> None:
    from websockets.asyncio.server import serve

    async with serve(
        lambda websocket: handle_connection(daemon, websocket),
        host,
        port,
        process_request=lambda connection, request: _authenticate_handshake(connection, request, auth_token),
        origins=ALLOWED_ORIGINS,
        max_size=MAX_MESSAGE_BYTES,
        ping_interval=20,
        ping_timeout=20,
    ):
        started.set()
        LOG.info("Windows UI WebSocket listening on %s:%s", host, port)
        await asyncio.Future()


def start_server(
    daemon: TranscriberDaemon,
    host: str = HOST,
    port: int = PORT,
    auth_token: str | None = None,
) -> threading.Thread:
    """Start the local UI bridge and fail if it cannot bind its loopback port."""
    started = threading.Event()
    startup_error: list[Exception] = []

    def run() -> None:
        try:
            asyncio.run(_serve(daemon, host, port, auth_token, started))
        except Exception as exc:
            if not started.is_set():
                startup_error.append(exc)
                started.set()
            else:
                LOG.exception("Windows UI WebSocket stopped unexpectedly")

    thread = threading.Thread(target=run, name="whisper-ui-websocket", daemon=True)
    thread.start()
    if not started.wait(timeout=10):
        raise RuntimeError(f"Windows UI WebSocket startup timed out on {host}:{port}")
    if startup_error:
        exc = startup_error[0]
        message = f"Windows UI WebSocket could not start on {host}:{port}: {exc}"
        LOG.error(message)
        raise RuntimeError(message) from exc
    return thread
