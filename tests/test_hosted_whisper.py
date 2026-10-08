from __future__ import annotations

import asyncio
import hashlib
import threading
import unittest

try:
    from websockets.asyncio.server import serve
except ImportError:  # pragma: no cover - server extra is optional
    serve = None  # type: ignore[assignment]

from transcriber.hosted_whisper import HostedWhisperEngine
from whisper_service.auth import TokenAuthenticator
from whisper_service.server import make_handler, make_process_request
from whisper_service.sessions import ModelProfile, SessionManager


class FakeWorkerSession:
    async def submit_audio(self, chunk: bytes) -> str | None:
        self.audio.append(chunk)
        return "hello from hosted whisper"

    async def finish(self) -> str:
        return "Hello from hosted Whisper."

    async def cancel(self) -> None:
        self.audio.clear()

    def __init__(self) -> None:
        self.audio: list[bytes] = []


class FakeWorker:
    def __init__(self) -> None:
        self.opened = asyncio.Event()
        self.sessions: list[FakeWorkerSession] = []

    async def open_session(self, profile: ModelProfile, language: str | None) -> FakeWorkerSession:
        session = FakeWorkerSession()
        self.sessions.append(session)
        self.opened.set()
        return session


@unittest.skipIf(serve is None, "install the server extra for hosted-client integration tests")
class HostedWhisperClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.token = "test-hosted-whisper-token"
        digest = hashlib.sha256(self.token.encode()).hexdigest()
        self.auth = TokenAuthenticator({digest: "desktop-test"})
        self.worker = FakeWorker()
        manager = SessionManager(self.worker, (ModelProfile("default", "base"),), device="cpu")
        self.server = await serve(
            make_handler(self.auth, manager), "127.0.0.1", 0,
            process_request=make_process_request(self.auth),
            max_size=64 * 1024,
            max_queue=8,
            compression=None,
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def test_streams_pcm_and_uses_hosted_final_transcript(self) -> None:
        partials: list[str] = []
        finals: list[str] = []
        errors: list[str] = []
        engine = HostedWhisperEngine(
            f"ws://127.0.0.1:{self.port}/v1/live", self.token, "default", "en",
            lambda _session, text, _generation: partials.append(text),
            lambda _session, text, _generation: finals.append(text),
            lambda _elapsed, _lag, error: errors.append(error) if error else None,
        )
        stop = threading.Event()
        thread = threading.Thread(target=engine.run, args=(stop, 1, "desktop-session"), daemon=True)
        thread.start()
        await asyncio.wait_for(self.worker.opened.wait(), timeout=3)
        audio = b"\x01\x00" * 320
        engine.submit_audio(audio)
        engine.close()
        await asyncio.to_thread(thread.join, 5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(self.worker.sessions[0].audio, [audio])
        self.assertEqual(partials, ["hello from hosted whisper"])
        self.assertEqual(finals, ["Hello from hosted Whisper."])
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
