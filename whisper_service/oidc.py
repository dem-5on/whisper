"""Strict RS256 ID-token verification for Google and Apple sign-in."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
import hmac
import json
import math
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa


_ISSUERS = {
    "google": frozenset({"https://accounts.google.com", "accounts.google.com"}),
    "apple": frozenset({"https://appleid.apple.com"}),
}
_JWKS_URLS = {
    "google": "https://www.googleapis.com/oauth2/v3/certs",
    "apple": "https://appleid.apple.com/auth/keys",
}
_MAX_TOKEN_BYTES = 32_768


class OIDCVerificationError(ValueError):
    """Provider ID token failed signature or claim validation."""


@dataclass(frozen=True)
class VerifiedOIDCIdentity:
    provider: str
    subject: str
    email: str | None
    email_verified: bool
    nonce: str


def verify_id_token(token: str, *, provider: str, client_id: str, nonce: str,
                    jwks: dict[str, Any], now: datetime | None = None) -> VerifiedOIDCIdentity:
    """Verify RS256 signature and essential OIDC claims against fetched JWKS."""
    if provider not in _ISSUERS:
        raise OIDCVerificationError("Unsupported identity provider")
    if not client_id or not nonce:
        raise OIDCVerificationError("OIDC client configuration is incomplete")
    if not isinstance(token, str) or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES:
        raise OIDCVerificationError("ID token is invalid")
    parts = token.split(".")
    if len(parts) != 3:
        raise OIDCVerificationError("ID token is malformed")
    header = _json_segment(parts[0])
    claims = _json_segment(parts[1])
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise OIDCVerificationError("ID token signing algorithm is not allowed")
    if header.get("crit") or header.get("b64") is False:
        raise OIDCVerificationError("Unsupported ID token header parameters")
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    if not isinstance(keys, list):
        raise OIDCVerificationError("Provider signing keys are unavailable")
    matches = [key for key in keys if isinstance(key, dict) and key.get("kid") == header["kid"]]
    if len(matches) != 1 or matches[0].get("kty") != "RSA" or matches[0].get("use", "sig") != "sig":
        raise OIDCVerificationError("ID token signing key is unknown")
    key = matches[0]
    try:
        public_key = rsa.RSAPublicNumbers(
            int.from_bytes(_b64url_decode(key["e"]), "big"),
            int.from_bytes(_b64url_decode(key["n"]), "big"),
        ).public_key()
        if public_key.key_size < 2048:
            raise ValueError("RSA signing key is too small")
        public_key.verify(
            _b64url_decode(parts[2]), f"{parts[0]}.{parts[1]}".encode("ascii"),
            padding.PKCS1v15(), hashes.SHA256(),
        )
    except Exception as exc:
        raise OIDCVerificationError("ID token signature is invalid") from exc

    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("OIDC verification time must include a timezone")
    epoch = current.timestamp()
    issuer = claims.get("iss")
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if issuer not in _ISSUERS[provider]:
        raise OIDCVerificationError("ID token issuer is invalid")
    if client_id not in audiences or any(not isinstance(item, str) for item in audiences):
        raise OIDCVerificationError("ID token audience is invalid")
    if len(audiences) > 1 and claims.get("azp") != client_id:
        raise OIDCVerificationError("ID token authorized party is invalid")
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(exp) or exp <= epoch:
        raise OIDCVerificationError("ID token has expired")
    issued_at = claims.get("iat")
    if (isinstance(issued_at, bool) or not isinstance(issued_at, (int, float)) or
            not math.isfinite(issued_at) or issued_at > epoch + 60):
        raise OIDCVerificationError("ID token issue time is invalid")
    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce, nonce):
        raise OIDCVerificationError("ID token nonce is invalid")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise OIDCVerificationError("ID token subject is invalid")
    email = claims.get("email")
    if email is not None and not isinstance(email, str):
        email = None
    email_verified = claims.get("email_verified") is True or claims.get("email_verified") == "true"
    return VerifiedOIDCIdentity(provider, subject, email, email_verified, token_nonce)


async def fetch_provider_jwks(provider: str, *, session: Any) -> dict[str, Any]:
    """Fetch a provider's public signing keys over HTTPS (caller supplies timeout)."""
    if provider not in _JWKS_URLS:
        raise OIDCVerificationError("Unsupported identity provider")
    try:
        async with session.get(_JWKS_URLS[provider], allow_redirects=False) as response:
            if response.status != 200:
                raise OIDCVerificationError("Provider signing keys could not be fetched")
            value = await response.json(content_type=None)
    except OIDCVerificationError:
        raise
    except Exception as exc:
        raise OIDCVerificationError("Provider signing keys could not be fetched") from exc
    if not isinstance(value, dict) or not isinstance(value.get("keys"), list):
        raise OIDCVerificationError("Provider returned malformed signing keys")
    return value


def _json_segment(segment: str) -> dict[str, Any]:
    try:
        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON member")
                result[key] = value
            return result

        value = json.loads(_b64url_decode(segment), object_pairs_hook=unique_object)
    except Exception as exc:
        raise OIDCVerificationError("ID token is malformed") from exc
    if not isinstance(value, dict):
        raise OIDCVerificationError("ID token is malformed")
    return value


def _b64url_decode(value: str) -> bytes:
    if (not isinstance(value, str) or not value or
            any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=" for ch in value) or
            "=" in value.rstrip("=") or len(value) - len(value.rstrip("=")) > 2):
        raise ValueError("invalid base64url value")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
