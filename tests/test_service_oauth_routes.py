from __future__ import annotations

import hashlib
import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

try:
    from aiohttp.test_utils import TestClient, TestServer
except ImportError:  # The hosted-service dev extra supplies aiohttp.
    TestClient = TestServer = None

from whisper_service.accounts import AccountStore
from whisper_service.auth import TokenAuthenticator
from whisper_service.diarization_api import OAUTH_CALLBACK_PATH, OAUTH_EXCHANGE_PATH, OAUTH_START_PATH, make_app
from whisper_service.entitlements import EntitlementStore
from whisper_service.oauth import OAuthTransactionStore
from whisper_service.oauth_flow import OAuthProviderConfig
from whisper_service.oidc import VerifiedOIDCIdentity


@unittest.skipIf(TestClient is None, "aiohttp test utilities are not installed")
class OAuthRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.accounts = AccountStore(root / "accounts.sqlite3")
        self.transactions = OAuthTransactionStore(root / "oauth.sqlite3")
        auth = TokenAuthenticator({hashlib.sha256(b"operator-token").hexdigest(): "operator"})
        app = make_app(
            auth, None, EntitlementStore(root / "entitlements.json"),
            oauth_transactions=self.transactions, account_store=self.accounts,
            oauth_providers={"google": OAuthProviderConfig(
                "google", "google-client", "https://auth.example.com/v1/auth/oauth/google/callback",
                client_secret="server-secret",
            )},
        )
        self.client = TestClient(TestServer(app))
        self.addAsyncCleanup(self.client.close)
        await self.client.start_server()

    async def test_oauth_start_callback_and_pkce_session_exchange(self) -> None:
        verifier = "desktop-verifier-0123456789-abcdefghijklmnopqrstuvwxyz"
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        response = await self.client.post(
            OAUTH_START_PATH.format(provider="google"), json={"code_challenge": challenge},
        )
        self.assertEqual(response.status, 200)
        authorization = (await response.json())["authorization_url"]
        query = parse_qs(urlparse(authorization).query)
        state = query["state"][0]
        pending = self.transactions.pending_callback(provider="google", state=state)
        identity = VerifiedOIDCIdentity("google", "subject-1", "person@example.com", True, pending.nonce)

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        with patch("aiohttp.ClientSession", return_value=FakeSession()), \
                patch("aiohttp.ClientTimeout", return_value=object()), \
                patch("whisper_service.diarization_api.exchange_and_verify", new=AsyncMock(return_value=identity)):
            callback = await self.client.get(
                f"{OAUTH_CALLBACK_PATH.format(provider='google')}?state={state}&code=provider-code",
                allow_redirects=False,
            )
        self.assertEqual(callback.status, 302)
        deep_link = urlparse(callback.headers["Location"])
        handoff = parse_qs(deep_link.query)["code"][0]
        bad = await self.client.post(OAUTH_EXCHANGE_PATH, json={"code": handoff, "code_verifier": "x" * 48})
        self.assertEqual(bad.status, 400)
        good = await self.client.post(OAUTH_EXCHANGE_PATH, json={"code": handoff, "code_verifier": verifier})
        self.assertEqual(good.status, 200)
        token = (await good.json())["access_token"]
        self.assertEqual(self.accounts.authenticate_session(token), self.accounts.resolve_verified_identity(
            provider="google", subject="subject-1",
        ).account_id)
        replay = await self.client.post(OAUTH_EXCHANGE_PATH, json={"code": handoff, "code_verifier": verifier})
        self.assertEqual(replay.status, 400)


if __name__ == "__main__":
    unittest.main()
