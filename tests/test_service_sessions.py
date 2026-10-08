from __future__ import annotations

import unittest

from whisper_service.sessions import ModelProfile, ServiceError, SessionLimits, SessionManager


class FakeWorkerSession:
    def __init__(self, partials: list[str | None] | None = None, final: str = "final words") -> None:
        self.partials = list(partials or [])
        self.final_text = final
        self.audio: list[bytes] = []
        self.cancelled = False
        self.finished = False
        self.fail_finish = False

    async def submit_audio(self, pcm_s16le: bytes) -> str | None:
        self.audio.append(pcm_s16le)
        return self.partials.pop(0) if self.partials else None

    async def finish(self) -> str:
        self.finished = True
        if self.fail_finish:
            raise RuntimeError("private inference failure")
        return self.final_text

    async def cancel(self) -> None:
        self.cancelled = True
        self.audio.clear()


class FakeWorker:
    def __init__(self) -> None:
        self.opened: list[FakeWorkerSession] = []
        self.fail_open = False

    async def open_session(self, profile: ModelProfile, language: str | None) -> FakeWorkerSession:
        if self.fail_open:
            raise RuntimeError("private runtime detail")
        session = FakeWorkerSession(["first hypothesis", "updated full hypothesis"])
        self.opened.append(session)
        return session


class SessionManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.worker = FakeWorker()
        self.profile = ModelProfile("default", "whisper-base")
        self.manager = SessionManager(self.worker, (self.profile,), device="cpu")

    async def test_partial_events_are_full_hypotheses_with_increasing_revisions(self) -> None:
        session = await self.manager.start("user-a", "default", "en")
        first = await session.submit_audio(b"\x01\x00" * 800)
        second = await session.submit_audio(b"\x02\x00" * 800)
        self.assertEqual(first, {"type": "transcript.partial", "revision": 1, "text": "first hypothesis"})
        self.assertEqual(second, {"type": "transcript.partial", "revision": 2, "text": "updated full hypothesis"})

    async def test_finish_returns_authoritative_final_and_releases_capacity(self) -> None:
        session = await self.manager.start("user-a", "default")
        event = await session.finish()
        self.assertEqual(event, {"type": "transcript.final", "text": "final words"})
        self.assertTrue(self.worker.opened[0].finished)
        self.assertEqual(await self.manager.active_count(), 0)
        with self.assertRaisesRegex(ServiceError, "already closed"):
            await session.submit_audio(b"\x00\x00")

    async def test_cancel_and_disconnect_discard_worker_state(self) -> None:
        session = await self.manager.start("user-a", "default")
        await session.submit_audio(b"\x00\x00" * 50)
        await session.disconnect()
        self.assertTrue(self.worker.opened[0].cancelled)
        self.assertEqual(self.worker.opened[0].audio, [])
        self.assertEqual(await self.manager.active_count(), 0)

    async def test_unknown_profile_and_unsupported_device_are_rejected(self) -> None:
        with self.assertRaisesRegex(ServiceError, "profile is unavailable"):
            await self.manager.start("user-a", "client-supplied-model-path")
        cuda_only = ModelProfile("cuda-only", "whisper-large", ("cuda",))
        manager = SessionManager(self.worker, (cuda_only,), device="cpu")
        with self.assertRaisesRegex(ServiceError, "not available on this server"):
            await manager.start("user-a", "cuda-only")

    async def test_per_user_and_global_concurrency_limits(self) -> None:
        limits = SessionLimits(max_concurrent_global=2, max_concurrent_per_user=1)
        manager = SessionManager(self.worker, (self.profile,), device="cpu", limits=limits)
        first = await manager.start("user-a", "default")
        with self.assertRaisesRegex(ServiceError, "(?i)your concurrent session limit"):
            await manager.start("user-a", "default")
        second = await manager.start("user-b", "default")
        with self.assertRaisesRegex(ServiceError, "(?i)service is at capacity"):
            await manager.start("user-c", "default")
        await first.cancel()
        third = await manager.start("user-c", "default")
        await second.cancel()
        await third.cancel()

    async def test_rolling_session_start_limit_applies_per_user_and_expires(self) -> None:
        now = [100.0]
        limits = SessionLimits(
            max_concurrent_global=1,
            max_starts_per_window=1,
            rate_window_seconds=60,
        )
        manager = SessionManager(self.worker, (self.profile,), device="cpu", limits=limits,
                                 clock=lambda: now[0])
        session = await manager.start("user-a", "default")
        await session.cancel()
        with self.assertRaisesRegex(ServiceError, "Session start limit reached") as raised:
            await manager.start("user-a", "default")
        self.assertEqual(raised.exception.code, "rate_limited")
        self.assertTrue(raised.exception.retryable)

        # The limit is keyed by authenticated user, not shared across users.
        other_user = await manager.start("user-b", "default")
        await other_user.cancel()
        now[0] += 60
        after_window = await manager.start("user-a", "default")
        await after_window.cancel()

    async def test_cpu_friendly_defaults(self) -> None:
        limits = SessionLimits()
        self.assertEqual(limits.max_audio_seconds, 300)
        self.assertEqual(limits.max_concurrent_global, 1)
        self.assertEqual(limits.max_starts_per_window, 10)
        self.assertEqual(limits.rate_window_seconds, 3600)

    async def test_max_audio_duration_cancels_and_releases_session(self) -> None:
        limits = SessionLimits(max_audio_seconds=1, max_audio_bytes=32000, max_chunk_bytes=32000)
        manager = SessionManager(self.worker, (self.profile,), device="cpu", limits=limits)
        session = await manager.start("user-a", "default")
        await session.submit_audio(b"\x00\x00" * 16000)
        with self.assertRaisesRegex(ServiceError, "exceeded the session duration"):
            await session.submit_audio(b"\x00\x00")
        self.assertTrue(self.worker.opened[0].cancelled)
        self.assertEqual(await manager.active_count(), 0)

    async def test_oversized_or_malformed_audio_is_rejected_without_worker_call(self) -> None:
        limits = SessionLimits(max_chunk_bytes=20)
        manager = SessionManager(self.worker, (self.profile,), device="cpu", limits=limits)
        session = await manager.start("user-a", "default")
        with self.assertRaisesRegex(ServiceError, "complete PCM16 samples"):
            await session.submit_audio(b"\x00")
        with self.assertRaisesRegex(ServiceError, "exceeds the accepted size"):
            await session.submit_audio(b"\x00\x00" * 11)
        self.assertEqual(self.worker.opened[0].audio, [])
        await session.cancel()

    async def test_worker_start_error_is_safe_and_releases_reservation(self) -> None:
        self.worker.fail_open = True
        with self.assertRaisesRegex(ServiceError, "worker could not start") as raised:
            await self.manager.start("user-a", "default")
        self.assertNotIn("private runtime detail", str(raised.exception))
        self.worker.fail_open = False
        session = await self.manager.start("user-a", "default")
        await session.cancel()

    async def test_final_worker_error_discards_worker_and_releases_capacity(self) -> None:
        session = await self.manager.start("user-a", "default")
        worker_session = self.worker.opened[0]
        worker_session.fail_finish = True
        with self.assertRaisesRegex(ServiceError, "Final transcription failed") as raised:
            await session.finish()
        self.assertNotIn("private inference failure", str(raised.exception))
        self.assertTrue(worker_session.cancelled)
        self.assertEqual(await self.manager.active_count(), 0)


if __name__ == "__main__":
    unittest.main()
