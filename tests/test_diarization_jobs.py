from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
import wave

from aiohttp.test_utils import TestClient, TestServer

from whisper_service.auth import TokenAuthenticator
from whisper_service.diarization_api import JOBS_PATH, make_app
from whisper_service.diarization_api import _validate_wav
from whisper_service.diarization_jobs import DiarizationJobManager
from whisper_service.entitlements import EntitlementStore, grant_feature, revoke_feature
from whisper_service.sessions import ServiceError


def wav_bytes(*, channels: int = 1, sample_width: int = 2, sample_rate: int = 16_000,
              frames: int = 160) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\0" * frames * channels * sample_width)
    return output.getvalue()


class DiarizationUploadValidationTests(unittest.TestCase):
    def test_accepts_expected_pcm_wav_and_rejects_other_audio_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "valid.wav"
            audio.write_bytes(wav_bytes())
            _validate_wav(audio)
            audio.write_bytes(wav_bytes(channels=2))
            with self.assertRaisesRegex(ValueError, "mono PCM16"):
                _validate_wav(audio)
            audio.write_bytes(b"not a wav")
            with self.assertRaisesRegex(ValueError, "valid WAV"):
                _validate_wav(audio)


class DiarizationJobManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_is_user_scoped_and_audio_is_deleted_after_processing(self) -> None:
        class Worker:
            async def diarize(self, _audio):
                return [{"start_seconds": 0.1, "end_seconds": 0.5, "speaker_id": "speaker_1"}]

        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "upload.wav"
            audio.write_bytes(wav_bytes())
            manager = DiarizationJobManager(Worker(), temp_dir=Path(directory))
            await manager.start()
            job_id = await manager.submit("user-a", audio)
            try:
                self.assertIsNone(await manager.get("user-b", job_id))
                for _ in range(100):
                    result = await manager.get("user-a", job_id)
                    if result and result["state"] in {"complete", "failed"}:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(result, {
                    "job_id": job_id,
                    "state": "complete",
                    "turns": [{"start_seconds": 0.1, "end_seconds": 0.5, "speaker_id": "speaker_1"}],
                })
                self.assertFalse(audio.exists())
            finally:
                await manager.close()

    async def test_rejects_jobs_when_bounded_capacity_is_full(self) -> None:
        started = asyncio.Event()
        finish = asyncio.Event()

        class Worker:
            async def diarize(self, _audio):
                started.set()
                await finish.wait()
                return []

        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.wav"
            second = Path(directory) / "second.wav"
            first.write_bytes(wav_bytes())
            second.write_bytes(wav_bytes())
            manager = DiarizationJobManager(Worker(), max_jobs=1)
            await manager.start()
            try:
                await manager.submit("user-a", first)
                await asyncio.wait_for(started.wait(), timeout=1)
                with self.assertRaisesRegex(ServiceError, "busy"):
                    await manager.submit("user-b", second)
                finish.set()
            finally:
                finish.set()
                await manager.close()


class DiarizationHttpApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        class Worker:
            async def diarize(self, _audio):
                return [{"start_seconds": 0.0, "end_seconds": 0.01, "speaker_id": "speaker_1"}]

        self.tempdir = tempfile.TemporaryDirectory()
        self.manager = DiarizationJobManager(Worker(), temp_dir=Path(self.tempdir.name))
        await self.manager.start()
        self.entitlement_path = Path(self.tempdir.name) / "entitlements.json"
        grant_feature(self.entitlement_path, "user-a", "diarization")
        grant_feature(self.entitlement_path, "user-b", "diarization")
        auth = TokenAuthenticator({
            hashlib.sha256(b"user-a-token").hexdigest(): "user-a",
            hashlib.sha256(b"user-b-token").hexdigest(): "user-b",
        })
        self.client = TestClient(TestServer(make_app(auth, self.manager, EntitlementStore(self.entitlement_path))))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.manager.close()
        self.tempdir.cleanup()

    async def test_requires_bearer_authentication(self) -> None:
        response = await self.client.post(JOBS_PATH, data=wav_bytes(), headers={"Content-Type": "audio/wav"})
        self.assertEqual(response.status, 401)

    async def test_requires_active_diarization_entitlement_before_reading_upload(self) -> None:
        revoke_feature(self.entitlement_path, "user-a", "diarization")
        response = await self.client.post(
            JOBS_PATH, data=wav_bytes(),
            headers={"Authorization": "Bearer user-a-token", "Content-Type": "audio/wav"},
        )
        self.assertEqual(response.status, 402)
        self.assertEqual((await response.json())["error"], "subscription_required")
        self.assertEqual(list(Path(self.tempdir.name).glob("wicx-diarization-*.wav")), [])

    async def test_fails_closed_when_entitlement_database_is_invalid(self) -> None:
        self.entitlement_path.write_text("not json", encoding="utf-8")
        response = await self.client.post(
            JOBS_PATH, data=wav_bytes(),
            headers={"Authorization": "Bearer user-a-token", "Content-Type": "audio/wav"},
        )
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"], "entitlement_unavailable")

    async def test_rejects_wrong_content_type_and_invalid_wav(self) -> None:
        headers = {"Authorization": "Bearer user-a-token"}
        wrong_type = await self.client.post(JOBS_PATH, data=b"x", headers=headers)
        self.assertEqual(wrong_type.status, 415)
        invalid_wav = await self.client.post(
            JOBS_PATH, data=b"not wav", headers={**headers, "Content-Type": "audio/wav"}
        )
        self.assertEqual(invalid_wav.status, 400)

    async def test_owner_can_submit_poll_and_other_user_gets_not_found(self) -> None:
        response = await self.client.post(
            JOBS_PATH, data=wav_bytes(),
            headers={"Authorization": "Bearer user-a-token", "Content-Type": "audio/wav"},
        )
        self.assertEqual(response.status, 202)
        created = await response.json()
        job_id = created["job_id"]
        self.assertEqual(created["state"], "queued")

        other_user = await self.client.get(
            f"{JOBS_PATH}/{job_id}", headers={"Authorization": "Bearer user-b-token"}
        )
        self.assertEqual(other_user.status, 404)

        for _ in range(100):
            result = await self.client.get(
                f"{JOBS_PATH}/{job_id}", headers={"Authorization": "Bearer user-a-token"}
            )
            payload = await result.json()
            if payload["state"] == "complete":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(payload["turns"], [
            {"start_seconds": 0.0, "end_seconds": 0.01, "speaker_id": "speaker_1"}
        ])

    async def test_unknown_job_ids_are_not_found(self) -> None:
        response = await self.client.get(
            f"{JOBS_PATH}/{'a' * 32}", headers={"Authorization": "Bearer user-a-token"}
        )
        self.assertEqual(response.status, 404)


class DiarizationCancellationTests(unittest.IsolatedAsyncioTestCase):
    """Cancelling must never hand back speaker turns for that recording."""

    async def asyncSetUp(self) -> None:
        release = asyncio.Event()
        self.release = release

        class GatedWorker:
            async def diarize(self, _audio):
                await release.wait()
                return [{"start_seconds": 0.0, "end_seconds": 1.0, "speaker_id": "speaker_1"}]

        self.tempdir = tempfile.TemporaryDirectory()
        self.manager = DiarizationJobManager(
            GatedWorker(), temp_dir=Path(self.tempdir.name), max_jobs=4,
        )
        await self.manager.start()
        self.entitlement_path = Path(self.tempdir.name) / "entitlements.json"
        grant_feature(self.entitlement_path, "user-a", "diarization")
        grant_feature(self.entitlement_path, "user-b", "diarization")
        auth = TokenAuthenticator({
            hashlib.sha256(b"user-a-token").hexdigest(): "user-a",
            hashlib.sha256(b"user-b-token").hexdigest(): "user-b",
        })
        self.client = TestClient(TestServer(make_app(auth, self.manager, EntitlementStore(self.entitlement_path))))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        self.release.set()
        await self.client.close()
        await self.manager.close()
        self.tempdir.cleanup()

    def _headers(self, user: str = "user-a") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {user}-token",
            "Content-Type": "audio/wav",
        }

    async def _submit(self, user: str = "user-a") -> tuple[str, Path]:
        response = await self.client.post(JOBS_PATH, data=wav_bytes(), headers=self._headers(user))
        self.assertEqual(response.status, 202)
        job_id = (await response.json())["job_id"]
        return job_id, self.manager.jobs[job_id].audio_path

    async def _await_state(self, job_id: str, wanted: set[str], user: str = "user-a") -> dict:
        payload: dict = {}
        for _ in range(200):
            status = await self.client.get(f"{JOBS_PATH}/{job_id}", headers=self._headers(user))
            payload = await status.json()
            if payload["state"] in wanted:
                return payload
            await asyncio.sleep(0.01)
        self.fail(f"job stayed in {payload.get('state')}")

    async def test_queued_job_cancels_without_ever_running(self) -> None:
        running_id, _ = await self._submit("user-a")
        await self._await_state(running_id, {"processing"})
        # A second user's job waits behind the blocked worker.
        queued_id, queued_audio = await self._submit("user-b")
        self.assertEqual(self.manager.jobs[queued_id].state, "queued")
        self.assertTrue(queued_audio.exists())

        impostor = await self.client.delete(
            f"{JOBS_PATH}/{queued_id}", headers={"Authorization": "Bearer user-a-token"}
        )
        self.assertEqual(impostor.status, 404)

        cancelled = await self.client.delete(
            f"{JOBS_PATH}/{queued_id}", headers={"Authorization": "Bearer user-b-token"}
        )
        self.assertEqual(cancelled.status, 200)
        self.assertEqual((await cancelled.json())["state"], "cancelled")
        self.assertFalse(queued_audio.exists())

        payload = await self._await_state(queued_id, {"cancelled"}, user="user-b")
        self.assertTrue(payload["cancelled"])
        self.assertNotIn("turns", payload)

        # The cancelled job never reaches inference, so the blocked job still holds the worker.
        self.assertEqual(self.manager.jobs[running_id].state, "processing")

    async def test_processing_job_cancels_and_discards_turns(self) -> None:
        job_id, audio = await self._submit()
        await self._await_state(job_id, {"processing"})
        cancelled = await self.client.delete(
            f"{JOBS_PATH}/{job_id}", headers={"Authorization": "Bearer user-a-token"}
        )
        self.assertEqual(cancelled.status, 200)
        self.release.set()
        payload = await self._await_state(job_id, {"cancelled"})
        self.assertNotIn("turns", payload)
        self.assertFalse(audio.exists())

    async def test_cancel_is_owner_scoped_and_terminal_jobs_conflict(self) -> None:
        job_id, _ = await self._submit()
        await self._await_state(job_id, {"processing"})
        other = await self.client.delete(
            f"{JOBS_PATH}/{job_id}", headers={"Authorization": "Bearer user-b-token"}
        )
        self.assertEqual(other.status, 404)
        unknown = await self.client.delete(
            f"{JOBS_PATH}/{'a' * 32}", headers={"Authorization": "Bearer user-a-token"}
        )
        self.assertEqual(unknown.status, 404)
        malformed = await self.client.delete(
            f"{JOBS_PATH}/not-a-job", headers={"Authorization": "Bearer user-a-token"}
        )
        self.assertEqual(malformed.status, 404)

        self.release.set()
        payload = await self._await_state(job_id, {"complete"})
        self.assertEqual(payload["turns"], [
            {"start_seconds": 0.0, "end_seconds": 1.0, "speaker_id": "speaker_1"}
        ])
        late = await self.client.delete(
            f"{JOBS_PATH}/{job_id}", headers={"Authorization": "Bearer user-a-token"}
        )
        self.assertEqual(late.status, 409)
        self.assertEqual((await late.json())["state"], "complete")


class DiarizationMeteringTests(unittest.IsolatedAsyncioTestCase):
    PERIOD_END = "2030-01-01T00:00:00+00:00"
    MAPPING = {"pro-monthly": (
        ("cloud_transcription", "diarization", "meeting_analysis"), 10,
    )}

    async def asyncSetUp(self) -> None:
        from datetime import UTC, datetime

        from whisper_service.billing import BillingStore

        class Worker:
            async def diarize(self, _audio):
                return []

        self.tempdir = tempfile.TemporaryDirectory()
        billing_path = Path(self.tempdir.name) / "billing.sqlite3"
        store = BillingStore(billing_path)
        store.apply_verified_event(
            provider="paystack", event_id="evt-1", subscription_id="SUB-1",
            user_id="user-a", plan_id="pro-monthly", status="active",
            current_period_end=self.PERIOD_END,
            now=datetime(2026, 10, 11, tzinfo=UTC),
        )
        self.store = BillingStore(billing_path)
        self.manager = DiarizationJobManager(Worker(), temp_dir=Path(self.tempdir.name))
        await self.manager.start()
        entitlement_path = Path(self.tempdir.name) / "entitlements.json"
        grant_feature(entitlement_path, "user-a", "diarization")
        auth = TokenAuthenticator({
            hashlib.sha256(b"user-a-token").hexdigest(): "user-a",
        })
        self.client = TestClient(TestServer(make_app(
            auth, self.manager, EntitlementStore(entitlement_path),
            billing_store=self.store, usage_plans=dict(self.MAPPING),
        )))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.manager.close()
        self.tempdir.cleanup()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer user-a-token"}

    async def test_submit_deducts_audio_minutes(self) -> None:
        response = await self.client.post(
            JOBS_PATH, data=wav_bytes(frames=16_000),
            headers={**self._headers(), "Content-Type": "audio/wav"},
        )
        self.assertEqual(response.status, 202)
        budget = self.store.cloud_usage_budget("user-a", dict(self.MAPPING))
        assert budget is not None
        self.assertEqual(budget["used_milliseconds"], 1000)
        self.assertEqual(budget["remaining_milliseconds"], 10 * 60_000 - 1000)

    async def test_exhausted_allowance_blocks_submit(self) -> None:
        self.store.record_cloud_usage("user-a", "pro-monthly", self.PERIOD_END, 10 * 60_000)
        response = await self.client.post(
            JOBS_PATH, data=wav_bytes(frames=160),
            headers={**self._headers(), "Content-Type": "audio/wav"},
        )
        self.assertEqual(response.status, 402)
        self.assertEqual((await response.json())["error"], "quota_exhausted")

if __name__ == "__main__":
    unittest.main()
