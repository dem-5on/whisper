from __future__ import annotations

import asyncio
import json
import unittest

try:
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
    from websockets.exceptions import InvalidStatus
except ImportError:  # pragma: no cover - exercised when server extra is absent
    connect = serve = None  # type: ignore[assignment]
    InvalidStatus = Exception  # type: ignore[assignment,misc]

from whisper_service.auth import TokenAuthenticator
from whisper_service.server import LIVE_PATH, make_handler, make_process_request
from whisper_service.sessions import ModelProfile, SessionManager


class FakeWorkerSession:
    def __init__(self) -> None:
        self.audio: list[bytes] = []
        self.cancelled = False

    async def submit_audio(self, chunk: bytes) -> str | None:
        self.audio.append(chunk)
        return "current hypothesis"

    async def finish(self) -> str:
        return "authoritative final"

    async def cancel(self) -> None:
        self.cancelled = True
        self.audio.clear()


class FakeWorker:
    def __init__(self) -> None:
        self.sessions: list[FakeWorkerSession] = []

    async def open_session(self, profile: ModelProfile, language: str | None) -> FakeWorkerSession:
        session = FakeWorkerSession()
        self.sessions.append(session)
        return session


@unittest.skipIf(connect is None or serve is None, "install the server extra for WebSocket tests")
class LiveWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.token = "test-token-for-local-integration"
        import hashlib

        auth = TokenAuthenticator({hashlib.sha256(self.token.encode()).hexdigest(): "user-a"})
        self.worker = FakeWorker()
        manager = SessionManager(self.worker, (ModelProfile("default", "whisper-base"),), device="cpu")
        self.server = await serve(
            make_handler(auth, manager), "127.0.0.1", 0,
            process_request=make_process_request(auth),
            max_size=64 * 1024,
            max_queue=8,
            compression=None,
        )
        self.port = self.server.sockets[0].getsockname()[1]
        self.uri = f"ws://127.0.0.1:{self.port}{LIVE_PATH}"

    async def asyncTearDown(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def test_authenticated_audio_stream_emits_partial_then_final(self) -> None:
        async with connect(self.uri, additional_headers={"Authorization": f"Bearer {self.token}"}) as socket:
            await socket.send(json.dumps({
                "type": "session.start", "profile": "default",
                "language": "en",
                "audio": {"encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1},
            }))
            ready = json.loads(await socket.recv())
            self.assertEqual(ready["type"], "session.ready")
            await socket.send(b"\x01\x00" * 100)
            partial = json.loads(await socket.recv())
            self.assertEqual((partial["type"], partial["revision"], partial["text"]),
                             ("transcript.partial", 1, "current hypothesis"))
            await socket.send('{"type":"session.finish"}')
            final = json.loads(await socket.recv())
            closed = json.loads(await socket.recv())
            self.assertEqual(final, {"type": "transcript.final", "text": "authoritative final"})
            self.assertEqual(closed["type"], "session.closed")
        self.assertEqual(self.worker.sessions[0].audio, [b"\x01\x00" * 100])

    async def test_unauthorized_client_is_rejected_during_handshake(self) -> None:
        with self.assertRaises(InvalidStatus) as raised:
            await connect(self.uri)
        self.assertEqual(raised.exception.response.status_code, 401)
        self.assertEqual(self.worker.sessions, [])

    async def test_health_endpoint_is_available_without_authentication(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        self.assertIn(b"200 OK", response)
        self.assertTrue(response.endswith(b"ok\n"))

    async def test_invalid_audio_format_is_rejected_before_worker_start(self) -> None:
        async with connect(self.uri, additional_headers={"Authorization": f"Bearer {self.token}"}) as socket:
            await socket.send(json.dumps({
                "type": "session.start", "profile": "default",
                "audio": {"encoding": "wav", "sample_rate_hz": 44100, "channels": 2},
            }))
            error = json.loads(await socket.recv())
            self.assertEqual(error["code"], "unsupported_audio")
        self.assertEqual(self.worker.sessions, [])

    async def test_cancel_discards_session_audio(self) -> None:
        async with connect(self.uri, additional_headers={"Authorization": f"Bearer {self.token}"}) as socket:
            await socket.send(json.dumps({
                "type": "session.start", "profile": "default",
                "audio": {"encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1},
            }))
            await socket.recv()  # ready
            await socket.send(b"\x01\x00" * 100)
            await socket.recv()  # partial
            await socket.send('{"type":"session.cancel"}')
            closed = json.loads(await socket.recv())
            self.assertEqual(closed["type"], "session.closed")
        self.assertTrue(self.worker.sessions[0].cancelled)
        self.assertEqual(self.worker.sessions[0].audio, [])


if __name__ == "__main__":
    unittest.main()
