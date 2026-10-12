from __future__ import annotations

from datetime import UTC, datetime
import base64
import json
import unittest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from whisper_service.oauth_flow import OAuthProviderConfig, _apple_client_secret, exchange_and_verify


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class _Response:
    def __init__(self, value, status=200):
        self.value = value
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, content_type=None):
        return self.value


class _Session:
    def __init__(self, token_response, jwks):
        self.token_response = token_response
        self.jwks = jwks
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return _Response(self.token_response)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return _Response(self.jwks)


class OAuthCodeExchangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_google_code_exchange_sends_server_secret_and_verifier_then_checks_token(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = key.public_key().public_numbers()
        jwks = {"keys": [{
            "kty": "RSA", "kid": "kid", "use": "sig",
            "n": _b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
            "e": _b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
        }]}
        claims = {
            "iss": "https://accounts.google.com", "aud": "google-client",
            "sub": "account-subject", "nonce": "nonce-from-start",
            "iat": datetime.now(UTC).timestamp(), "exp": datetime.now(UTC).timestamp() + 120,
        }
        first, second = _b64(json.dumps({"alg": "RS256", "kid": "kid"}).encode()), _b64(json.dumps(claims).encode())
        signature = key.sign(f"{first}.{second}".encode(), padding.PKCS1v15(), hashes.SHA256())
        token = f"{first}.{second}.{_b64(signature)}"
        session = _Session({"id_token": token}, jwks)
        config = OAuthProviderConfig(
            "google", "google-client", "https://auth.example.com/callback", client_secret="server-only-secret",
        )
        identity = await exchange_and_verify(
            config=config, code="single-use-code", provider_pkce_verifier="provider-verifier",
            nonce="nonce-from-start", session=session,
        )
        self.assertEqual(identity.subject, "account-subject")
        endpoint, options = session.post_calls[0]
        self.assertEqual(endpoint, "https://oauth2.googleapis.com/token")
        self.assertEqual(options["data"]["client_secret"], "server-only-secret")
        self.assertEqual(options["data"]["code_verifier"], "provider-verifier")
        self.assertEqual(session.get_calls[0][0], "https://www.googleapis.com/oauth2/v3/certs")

    async def test_apple_client_secret_is_short_lived_es256_jwt(self):
        private_key = ec.generate_private_key(ec.SECP256R1())
        pem = private_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ).decode()
        config = OAuthProviderConfig(
            "apple", "apple-service", "https://auth.example.com/apple",
            apple_team_id="team-id", apple_key_id="key-id", apple_private_key=pem,
        )
        header, claims, signature = _apple_client_secret(config).split(".")
        self.assertEqual(json.loads(base64.urlsafe_b64decode(header + "=="))["alg"], "ES256")
        payload = json.loads(base64.urlsafe_b64decode(claims + "=="))
        self.assertEqual(payload["iss"], "team-id")
        self.assertEqual(payload["sub"], "apple-service")
        self.assertLessEqual(payload["exp"] - payload["iat"], 300)
        self.assertEqual(len(base64.urlsafe_b64decode(signature + "==")), 64)


if __name__ == "__main__":
    unittest.main()
