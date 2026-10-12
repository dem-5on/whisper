from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer

from whisper_service.auth import TokenAuthenticator
from whisper_service.diarization_api import REWRITE_MODELS_PATH, REWRITE_PATH, make_app
from whisper_service.entitlements import EntitlementStore, grant_feature
from whisper_service.rewrite import OpenRouterRewriteModel, RewriteError


class FakeRewriteModel:
    models = ("vendor/fast-text", "vendor/quality-text")

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def rewrite(self, text: str, *, model: str | None = None) -> tuple[str, str]:
        self.calls.append((text, model))
        return "Cleaned transcript.", model or self.models[0]


class RewriteApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        entitlement_file = root / "entitlements.json"
        grant_feature(entitlement_file, "user-a", "cloud_rewrite")
        auth = TokenAuthenticator({
            hashlib.sha256(b"user-a-token").hexdigest(): "user-a",
            hashlib.sha256(b"user-b-token").hexdigest(): "user-b",
        })
        self.model = FakeRewriteModel()
        self.client = TestClient(TestServer(make_app(
            auth, None, EntitlementStore(entitlement_file), rewrite_model=self.model,
        )))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.tempdir.cleanup()

    async def test_auth_entitlement_and_model_selection_are_enforced(self) -> None:
        denied_auth = await self.client.post(REWRITE_PATH, json={"text": "hello"})
        self.assertEqual(denied_auth.status, 401)
        denied_plan = await self.client.post(
            REWRITE_PATH, json={"text": "hello"},
            headers={"Authorization": "Bearer user-b-token"},
        )
        self.assertEqual(denied_plan.status, 402)
        self.assertEqual(self.model.calls, [])

        catalog = await self.client.get(
            REWRITE_MODELS_PATH, headers={"Authorization": "Bearer user-a-token"},
        )
        self.assertEqual(await catalog.json(), {
            "models": ["vendor/fast-text", "vendor/quality-text"],
            "default_model": "vendor/fast-text",
        })
        unsupported = await self.client.post(
            REWRITE_PATH, json={"text": "hello", "model": "vendor/not-allowed"},
            headers={"Authorization": "Bearer user-a-token"},
        )
        self.assertEqual(unsupported.status, 400)

        response = await self.client.post(
            REWRITE_PATH, json={"text": "uh, please clean this", "model": "vendor/quality-text"},
            headers={"Authorization": "Bearer user-a-token"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"text": "Cleaned transcript.", "model": "vendor/quality-text"})
        self.assertEqual(self.model.calls, [("uh, please clean this", "vendor/quality-text")])

    async def test_rejects_empty_and_oversized_text(self) -> None:
        headers = {"Authorization": "Bearer user-a-token"}
        for text in ("", " " * 10, "x" * 20_001):
            response = await self.client.post(REWRITE_PATH, json={"text": text}, headers=headers)
            self.assertEqual(response.status, 400)


class OpenRouterRewriteModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_calls_openrouter_with_server_key_and_minimal_editing_prompt(self) -> None:
        from unittest.mock import patch

        captured: dict[str, object] = {}

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def json(self, **_kwargs):
                return {"choices": [{"message": {"content": "Corrected words."}}]}

        class Session:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def post(self, url, **kwargs):
                captured["url"] = url
                captured.update(kwargs)
                return Response()

        model = OpenRouterRewriteModel("server-secret", ("vendor/fast-text", "vendor/quality-text"))
        with patch("aiohttp.ClientSession", Session):
            result = await model.rewrite("um, fix my transcript", model="vendor/quality-text")

        self.assertEqual(result, ("Corrected words.", "vendor/quality-text"))
        self.assertEqual(captured["url"], OpenRouterRewriteModel.endpoint)
        self.assertEqual(captured["headers"], {
            "Authorization": "Bearer server-secret", "Content-Type": "application/json",
        })
        payload = captured["json"]
        self.assertEqual(payload["model"], "vendor/quality-text")
        self.assertEqual(payload["temperature"], 0)
        self.assertIn("Do not translate", payload["messages"][0]["content"])

    async def test_only_configured_models_can_be_used(self) -> None:
        model = OpenRouterRewriteModel("server-secret", ("vendor/fast-text",))
        with self.assertRaisesRegex(RewriteError, "not enabled"):
            await model.rewrite("text", model="someone/unapproved")


if __name__ == "__main__":
    unittest.main()
