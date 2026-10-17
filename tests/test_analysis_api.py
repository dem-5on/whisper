from __future__ import annotations

import hashlib
from datetime import UTC, datetime
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from whisper_service.auth import TokenAuthenticator
from whisper_service.billing import BillingStore
from whisper_service.diarization_api import ANALYSIS_KINDS_PATH, ANALYSIS_PATH, make_app
from whisper_service.entitlements import EntitlementStore, grant_feature


class FakeAnalysisModel:
    models = ("vendor/analysis-mini",)

    def __init__(self, payload: str = '{"title": "Intro"}') -> None:
        self.calls: list[tuple[str, str]] = []
        self.payload = payload

    async def analyze(self, kind, text, *, language=None, model=None):
        self.calls.append((kind, text))
        return self.payload, model or self.models[0]


def _auth() -> TokenAuthenticator:
    return TokenAuthenticator({
        hashlib.sha256(b"user-a-token").hexdigest(): "user-a",
        hashlib.sha256(b"user-b-token").hexdigest(): "user-b",
    })


class AnalysisApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        entitlement_file = root / "entitlements.json"
        grant_feature(entitlement_file, "user-a", "lecture_analysis")
        grant_feature(entitlement_file, "user-a", "translation")
        self.model = FakeAnalysisModel('[{"title": "Intro", "summary": "Start"}]')
        self.client = TestClient(TestServer(make_app(
            _auth(), None, EntitlementStore(entitlement_file),
            analysis_model=self.model,
        )))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.tempdir.cleanup()

    async def test_auth_entitlement_and_input_are_enforced(self) -> None:
        denied_auth = await self.client.post(ANALYSIS_PATH, json={"kind": "topics", "text": "hi"})
        self.assertEqual(denied_auth.status, 401)
        denied_plan = await self.client.post(
            ANALYSIS_PATH, json={"kind": "topics", "text": "hi"},
            headers={"Authorization": "Bearer user-b-token"},
        )
        self.assertEqual(denied_plan.status, 402)
        self.assertEqual((await denied_plan.json())["error"], "subscription_required")
        self.assertEqual(self.model.calls, [])
        bad_kind = await self.client.post(
            ANALYSIS_PATH, json={"kind": "teleport", "text": "hi"},
            headers={"Authorization": "Bearer user-a-token"},
        )
        self.assertEqual(bad_kind.status, 400)

    async def test_topics_return_parsed_and_validated(self) -> None:
        response = await self.client.post(
            ANALYSIS_PATH, json={"kind": "topics", "text": "transcript words here"},
            headers={"Authorization": "Bearer user-a-token"},
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["kind"], "topics")
        self.assertEqual(body["result"], [{
            "title": "Intro", "summary": "Start",
            "start_segment": 0, "end_segment": 0,
        }])
        self.assertEqual(self.model.calls, [("topics", "transcript words here")])

    async def test_kinds_catalog_lists_entitled_kinds(self) -> None:
        catalog = await self.client.get(
            ANALYSIS_KINDS_PATH, headers={"Authorization": "Bearer user-a-token"},
        )
        kinds = {entry["kind"] for entry in (await catalog.json())["kinds"]}
        self.assertIn("notes", kinds)
        self.assertIn("translation", kinds)
        self.assertNotIn("summary", kinds)


class AnalysisQuotaTests(unittest.IsolatedAsyncioTestCase):
    PERIOD_END = "2030-01-01T00:00:00+00:00"
    MAPPING = {"pro-monthly": (
        ("cloud_transcription", "lecture_analysis", "translation"), 600,
    )}

    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        entitlement_file = root / "entitlements.json"
        grant_feature(entitlement_file, "user-a", "lecture_analysis")
        billing_path = root / "billing.sqlite3"
        store = BillingStore(billing_path)
        store.apply_verified_event(
            provider="paystack", event_id="evt-1", subscription_id="SUB-1",
            user_id="user-a", plan_id="pro-monthly", status="active",
            current_period_end=self.PERIOD_END,
            now=datetime(2026, 10, 11, tzinfo=UTC),
        )
        self.store = BillingStore(billing_path)
        self.model = FakeAnalysisModel('"# Notes"')
        self.client = TestClient(TestServer(make_app(
            _auth(), None, EntitlementStore(entitlement_file),
            billing_store=self.store,
            usage_plans=dict(self.MAPPING),
            analysis_model=self.model,
            analysis_cost_ms=60_000,
        )))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.tempdir.cleanup()

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer user-a-token"}

    async def test_success_deducts_one_stage_cost(self) -> None:
        response = await self.client.post(
            ANALYSIS_PATH, json={"kind": "notes", "text": "words"},
            headers=self._auth_headers(),
        )
        self.assertEqual(response.status, 200)
        budget = self.store.cloud_usage_budget("user-a", dict(self.MAPPING))
        assert budget is not None
        self.assertEqual(budget["used_milliseconds"], 60_000)
        self.assertEqual(budget["remaining_milliseconds"], 600 * 60_000 - 60_000)

    async def test_exhausted_allowance_blocks_without_charging(self) -> None:
        self.store.record_cloud_usage("user-a", "pro-monthly", self.PERIOD_END, 600 * 60_000)
        response = await self.client.post(
            ANALYSIS_PATH, json={"kind": "notes", "text": "words"},
            headers=self._auth_headers(),
        )
        self.assertEqual(response.status, 402)
        self.assertEqual((await response.json())["error"], "quota_exhausted")
        self.assertEqual(self.model.calls, [])
