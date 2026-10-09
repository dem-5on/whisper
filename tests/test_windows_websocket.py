import asyncio
import json

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidStatus


from transcriber.platforms.windows.websocket import (
    ALLOWED_ORIGINS,
    _authenticate_handshake,
    _valid_ui_token,
    handle_connection,
    start_server,
)


class FakeDaemon:
    def __init__(self):
        self.commands = []

    def command(self, command, params):
        self.commands.append((command, params))
        return {"ok": "true", "state": "IDLE", "command": command}

    def status_dict(self):
        return {"state": "IDLE", "live_revision": 1}

    def wait_events_since(self, since, session_id=None, timeout=1.0):
        if since < 1:
            return ([{"type": "partial", "revision": 1, "text": "hello"}], 1)
        raise RuntimeError("end test event stream")


class FakeWebSocket:
    def __init__(self):
        self.messages = [
            json.dumps({"command": "set_mic", "device": "Default", "request_id": 7}),
            json.dumps({"command": "set_key", "provider": "openai", "key": "secret-for-test", "request_id": 8}),
            json.dumps({"command": "set_live_engine", "engine": "hosted-whisper", "server_url": "wss://whisper.example/v1/live", "request_id": 9}),
            json.dumps({"command": "set_live_model", "model": "default", "request_id": 10}),
            json.dumps({"command": "subscribe", "since": 0, "request_id": 11}),
        ]
        self.sent = []
        self.event_sent = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.messages:
            return self.messages.pop(0)
        await self.event_sent.wait()
        raise StopAsyncIteration

    async def send(self, message):
        payload = json.loads(message)
        self.sent.append(payload)
        if payload.get("type") == "daemon_event":
            self.event_sent.set()


def test_windows_websocket_commands_events_and_origin_restriction(monkeypatch):
    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", run_inline)

    async def run():
        daemon = FakeDaemon()
        websocket = FakeWebSocket()
        await handle_connection(daemon, websocket)
        assert daemon.commands == [
            ("set_mic", {"device": "Default"}),
            ("set_key", {"provider": "openai", "key": "secret-for-test"}),
            ("set_live_engine", {"engine": "hosted-whisper", "server_url": "wss://whisper.example/v1/live"}),
            ("set_live_model", {"model": "default"}),
        ]
        assert websocket.sent[0]["request_id"] == 7
        assert websocket.sent[0]["command"] == "set_mic"
        assert websocket.sent[1]["request_id"] == 8
        assert websocket.sent[2]["request_id"] == 9
        assert websocket.sent[3]["request_id"] == 10
        assert websocket.sent[4]["request_id"] == 11 and websocket.sent[4]["subscribed"] is True
        assert websocket.sent[5]["type"] == "status"
        assert websocket.sent[6]["event"]["text"] == "hello"

    asyncio.run(run())

    assert "http://tauri.localhost" in ALLOWED_ORIGINS
    assert "https://attacker.example" not in ALLOWED_ORIGINS


def test_windows_websocket_requires_a_matching_per_launch_token():
    assert _valid_ui_token("/", None)
    assert _valid_ui_token("/?token=secret-123", "secret-123")
    assert not _valid_ui_token("/", "secret-123")
    assert not _valid_ui_token("/?token=wrong", "secret-123")
    assert not _valid_ui_token("/?token=%E2%98%83", "secret-123")
    assert not _valid_ui_token("/?token=secret-123&token=secret-123", "secret-123")


def test_windows_websocket_authenticates_before_upgrade():
    class Connection:
        def respond(self, status, message):
            return status, message

    class Request:
        def __init__(self, path):
            self.path = path

    connection = Connection()
    assert _authenticate_handshake(connection, Request("/?token=secret"), "secret") is None
    assert _authenticate_handshake(connection, Request("/"), "secret") == (401, "Unauthorized\n")


def test_windows_websocket_startup_failure_is_reported_to_the_daemon(monkeypatch):
    async def fail_to_bind(_daemon, _host, _port, _token, _started):
        raise OSError("address already in use")

    monkeypatch.setattr("transcriber.platforms.windows.websocket._serve", fail_to_bind)
    with pytest.raises(RuntimeError, match="could not start.*address already in use"):
        start_server(FakeDaemon(), host="127.0.0.1", port=47651, auth_token="private-token")


def test_websocket_server_enforces_browser_origin(monkeypatch):
    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", run_inline)

    async def run():
        daemon = FakeDaemon()
        server = serve(
            lambda websocket: handle_connection(daemon, websocket),
            "127.0.0.1",
            0,
            origins=ALLOWED_ORIGINS,
        )
        try:
            await server.__aenter__()
        except OSError as exc:
            pytest.skip(f"sandbox does not permit loopback listeners: {exc}")
        try:
            port = server.sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{port}"
            async with connect(url, origin="http://tauri.localhost") as websocket:
                await websocket.send(json.dumps({"command": "status", "request_id": 1}))
                assert json.loads(await websocket.recv())["request_id"] == 1
            with pytest.raises(InvalidStatus):
                async with connect(url, origin="https://attacker.example"):
                    pass
        finally:
            await server.__aexit__(None, None, None)

    asyncio.run(run())
