"""Server-side authorization-code exchange for Google and Apple OIDC."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import json
import time
from typing import Any
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from .oidc import OIDCVerificationError, VerifiedOIDCIdentity, fetch_provider_jwks, verify_id_token


@dataclass(frozen=True)
class OAuthProviderConfig:
    provider: str
    client_id: str
    redirect_uri: str
    client_secret: str | None = None
    apple_team_id: str | None = None
    apple_key_id: str | None = None
    apple_private_key: str | None = None

    def validate(self) -> None:
        if self.provider not in {"google", "apple"} or not self.client_id:
            raise ValueError("OAuth provider configuration is invalid")
        redirect = urlparse(self.redirect_uri)
        if (redirect.scheme != "https" or not redirect.hostname or redirect.username or
                redirect.password or redirect.fragment):
            raise ValueError("OAuth redirect URI must use HTTPS")
        if self.provider == "google" and not self.client_secret:
            raise ValueError("Google OAuth client secret is required")
        if self.provider == "apple" and not all((self.apple_team_id, self.apple_key_id, self.apple_private_key)):
            raise ValueError("Apple Sign in configuration is incomplete")


async def exchange_and_verify(*, config: OAuthProviderConfig, code: str,
                              provider_pkce_verifier: str, nonce: str,
                              session: Any) -> VerifiedOIDCIdentity:
    """Exchange a provider authorization code and validate its returned ID token."""
    config.validate()
    if not isinstance(code, str) or not code or len(code) > 4096:
        raise OIDCVerificationError("Authorization code is invalid")
    if not isinstance(provider_pkce_verifier, str) or not provider_pkce_verifier:
        raise OIDCVerificationError("Authorization code proof is missing")
    token_endpoint = {
        "google": "https://oauth2.googleapis.com/token",
        "apple": "https://appleid.apple.com/auth/token",
    }[config.provider]
    form = {
        "client_id": config.client_id,
        "code": code,
        "code_verifier": provider_pkce_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": config.redirect_uri,
    }
    if config.provider == "google":
        form["client_secret"] = config.client_secret or ""
    else:
        form["client_secret"] = _apple_client_secret(config)
    try:
        async with session.post(token_endpoint, data=form, allow_redirects=False) as response:
            if response.status != 200:
                raise OIDCVerificationError("Provider authorization code exchange failed")
            token_result = await response.json(content_type=None)
    except OIDCVerificationError:
        raise
    except Exception as exc:
        raise OIDCVerificationError("Provider authorization code exchange failed") from exc
    id_token = token_result.get("id_token") if isinstance(token_result, dict) else None
    if not isinstance(id_token, str):
        raise OIDCVerificationError("Provider response did not contain an ID token")
    jwks = await fetch_provider_jwks(config.provider, session=session)
    return verify_id_token(
        id_token, provider=config.provider, client_id=config.client_id,
        nonce=nonce, jwks=jwks,
    )


def _apple_client_secret(config: OAuthProviderConfig) -> str:
    """Create Apple's short-lived ES256 client-secret JWT on the server."""
    try:
        key = serialization.load_pem_private_key(
            (config.apple_private_key or "").encode("utf-8"), password=None,
        )
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError("Apple key must be an ES256 P-256 private key")
        header = {"alg": "ES256", "kid": config.apple_key_id, "typ": "JWT"}
        issued = int(time.time())
        claims = {
            "iss": config.apple_team_id,
            "iat": issued,
            "exp": issued + 300,
            "aud": "https://appleid.apple.com",
            "sub": config.client_id,
        }
        first = _b64(json.dumps(header, separators=(",", ":")).encode())
        second = _b64(json.dumps(claims, separators=(",", ":")).encode())
        signing_input = f"{first}.{second}".encode("ascii")
        der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r_value, s_value = decode_dss_signature(der)
        signature = r_value.to_bytes(32, "big") + s_value.to_bytes(32, "big")
        return f"{first}.{second}.{_b64(signature)}"
    except Exception as exc:
        raise ValueError("Could not create Apple client secret") from exc


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
