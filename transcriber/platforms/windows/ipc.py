"""Authenticated per-user named-pipe IPC for the Windows daemon."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener
from pathlib import Path
import secrets
import threading
from typing import Any, Iterator

from .paths import data_dir

LOG = logging.getLogger("transcriber.windows.ipc")
PIPE_NAME = r"\\.\pipe\Whisper-" + hashlib.sha256(
    os.environ.get("USERPROFILE", str(Path.home())).casefold().encode("utf-8")
).hexdigest()[:20]
MAX_REQUEST_BYTES = 8192


def _authkey() -> bytes:
    """Load/create the local pipe secret in the current user's data directory."""
    path = data_dir() / "ipc.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        try:
            with path.open("xb") as key_file:
                key_file.write(key)
        except FileExistsError:
            key = path.read_bytes()
        else:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        return key


def request(payload: dict[str, Any], timeout: float = 2.0) -> dict[str, Any]:
    # AF_PIPE uses Windows' named-pipe connect API, which has no socket-style
    # timeout parameter. Failed connections to a missing pipe fail promptly.
    del timeout
    try:
        with Client(PIPE_NAME, family="AF_PIPE", authkey=_authkey()) as connection:
            connection.send_bytes(json.dumps(payload).encode("utf-8"))
            return json.loads(connection.recv_bytes(65536))
    except AuthenticationError as exc:
        raise OSError("Whisper named-pipe authentication failed") from exc


def stream(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    try:
        connection = Client(PIPE_NAME, family="AF_PIPE", authkey=_authkey())
    except AuthenticationError as exc:
        raise OSError("Whisper named-pipe authentication failed") from exc
    with connection:
        connection.send_bytes(json.dumps(payload).encode("utf-8"))
        while True:
            try:
                yield json.loads(connection.recv_bytes(65536))
            except (EOFError, OSError):
                return


class WindowsPipeServer:
    """Threaded request server; Unix platforms keep using socketserver."""

    def __init__(self, daemon: Any) -> None:
        self.daemon = daemon
        self._listener = Listener(PIPE_NAME, family="AF_PIPE", authkey=_authkey())
        self._stopping = threading.Event()

    def serve_forever(self) -> None:
        try:
            while not self._stopping.is_set():
                try:
                    connection = self._listener.accept()
                except AuthenticationError:
                    LOG.warning("Rejected unauthenticated Windows named-pipe client")
                    continue
                except (OSError, EOFError):
                    if self._stopping.is_set():
                        break
                    raise
                threading.Thread(
                    target=self._handle, args=(connection,),
                    name="whisper-pipe-client", daemon=True,
                ).start()
        finally:
            self._listener.close()

    def _handle(self, connection: Any) -> None:
        try:
            request = json.loads(connection.recv_bytes(MAX_REQUEST_BYTES))
            if not isinstance(request, dict) or not isinstance(request.get("command"), str):
                connection.send_bytes(b'{"ok":"false","error":"Invalid request"}')
                return
            command = request["command"]
            if command == "_wake":
                connection.send_bytes(b'{"ok":"true"}')
            elif command == "shutdown":
                connection.send_bytes(b'{"ok":"true","state":"STOPPING"}')
                threading.Thread(target=self.shutdown, name="whisper-pipe-shutdown", daemon=True).start()
            elif command == "subscribe":
                self._subscribe(connection, request)
            else:
                params = {key: value for key, value in request.items() if key != "command"}
                response = self.daemon.command(command, params)
                connection.send_bytes(json.dumps(response).encode("utf-8"))
        except (ValueError, OSError, EOFError):
            LOG.debug("Named-pipe request ended or was invalid", exc_info=True)
        finally:
            connection.close()

    def _subscribe(self, connection: Any, request: dict[str, Any]) -> None:
        try:
            last = max(0, int(request.get("since", 0)))
        except (TypeError, ValueError):
            last = 0
        session_id = request.get("session_id")
        session_id = str(session_id) if session_id else None
        connection.send_bytes(json.dumps({"ok": "true", "subscribed": True, "since": last}).encode())
        while not self._stopping.is_set():
            events, revision = self.daemon.wait_events_since(last, session_id, timeout=1.0)
            for event in events:
                connection.send_bytes(json.dumps(event).encode("utf-8"))
                last = int(event.get("revision", last))
            last = max(last, revision)

    def shutdown(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()

        def wake_accept() -> None:
            try:
                with Client(PIPE_NAME, family="AF_PIPE", authkey=_authkey()) as connection:
                    connection.send_bytes(b'{"command":"_wake"}')
                    connection.recv_bytes(1024)
            except (OSError, EOFError):
                pass

        threading.Thread(target=wake_accept, name="whisper-pipe-wakeup", daemon=True).start()

    def server_close(self) -> None:
        self.shutdown()
