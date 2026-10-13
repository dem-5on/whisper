from __future__ import annotations

from datetime import UTC, datetime
import base64
import json
import unittest

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from whisper_service.oidc import OIDCVerificationError, verify_id_token


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class OIDCTokenVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = cls.private_key.public_key().public_numbers()
        cls.jwks = {"keys": [{
            "kty": "RSA", "kid": "test-key", "use": "sig", "alg": "RS256",
            "n": _b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
            "e": _b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
        }]}
        cls.now = datetime(2026, 10, 12, 12, tzinfo=UTC)

    def _token(self, claims: dict, *, alg: str = "RS256", key: str = "test-key") -> str:
        header = _b64(json.dumps({"alg": alg, "kid": key}).encode())
        payload = _b64(json.dumps(claims).encode())
        signing_input = f"{header}.{payload}".encode("ascii")
        signature = self.private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{payload}.{_b64(signature)}"

    def _claims(self, **overrides: object) -> dict:
        claims = {
            "iss": "https://accounts.google.com", "aud": "desktop-client",
            "sub": "stable-provider-subject", "nonce": "expected-nonce",
            "iat": self.now.timestamp() - 10, "exp": self.now.timestamp() + 300,
            "email": "person@example.com", "email_verified": True,
        }
        claims.update(overrides)
        return claims

    def test_verifies_signature_issuer_audience_nonce_and_identity_claims(self) -> None:
        identity = verify_id_token(
            self._token(self._claims()), provider="google", client_id="desktop-client",
            nonce="expected-nonce", jwks=self.jwks, now=self.now,
        )
        self.assertEqual(identity.provider, "google")
        self.assertEqual(identity.subject, "stable-provider-subject")
        self.assertEqual(identity.email, "person@example.com")
        self.assertTrue(identity.email_verified)

    def test_rejects_wrong_issuer_audience_nonce_expiry_and_algorithm(self) -> None:
        cases = (
            (self._claims(iss="https://attacker.example"), "RS256"),
            (self._claims(aud="other-client"), "RS256"),
            (self._claims(nonce="wrong"), "RS256"),
            (self._claims(exp=self.now.timestamp()), "RS256"),
            (self._claims(), "none"),
        )
        for claims, algorithm in cases:
            with self.subTest(claims=claims, algorithm=algorithm), self.assertRaises(OIDCVerificationError):
                verify_id_token(
                    self._token(claims, alg=algorithm), provider="google", client_id="desktop-client",
                    nonce="expected-nonce", jwks=self.jwks, now=self.now,
                )

    def test_rejects_wrong_signature_key_and_ambiguous_authorized_party(self) -> None:
        token = self._token(self._claims())
        signature = token.rsplit(".", 1)[1]
        altered_signature = ("A" if signature[0] != "A" else "B") + signature[1:]
        altered = token.rsplit(".", 1)[0] + "." + altered_signature
        for candidate in (altered, self._token(self._claims(), key="unknown"),
                          self._token(self._claims(aud=["desktop-client", "other"]))):
            with self.subTest(candidate=candidate[:20]), self.assertRaises(OIDCVerificationError):
                verify_id_token(
                    candidate, provider="google", client_id="desktop-client",
                    nonce="expected-nonce", jwks=self.jwks, now=self.now,
                )

    def test_apple_issuer_and_optional_unverified_email(self) -> None:
        claims = self._claims(
            iss="https://appleid.apple.com", aud="apple-service-id",
            email="private-relay@example.com", email_verified="true",
        )
        identity = verify_id_token(
            self._token(claims), provider="apple", client_id="apple-service-id",
            nonce="expected-nonce", jwks=self.jwks, now=self.now,
        )
        self.assertEqual(identity.provider, "apple")
        self.assertTrue(identity.email_verified)
        with self.assertRaises(OIDCVerificationError):
            verify_id_token(
                self._token(self._claims()), provider="apple", client_id="apple-service-id",
                nonce="expected-nonce", jwks=self.jwks, now=self.now,
            )


if __name__ == "__main__":
    unittest.main()
