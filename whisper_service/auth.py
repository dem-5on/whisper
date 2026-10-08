"""Invite-only bearer-token authentication for the hosted service."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Mapping


class AuthenticationConfigError(RuntimeError):
    """The service credential database is missing or invalid."""


class TokenAuthenticator:
    """Authenticates opaque user tokens stored only as SHA-256 digests."""

    def __init__(self, token_digests: Mapping[str, str]) -> None:
        normalized: dict[str, str] = {}
        for digest, user_id in token_digests.items():
            value = digest.lower()
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise AuthenticationConfigError("Token database contains an invalid token digest")
            if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 128:
                raise AuthenticationConfigError("Token database contains an invalid user ID")
            normalized[value] = user_id
        self._token_digests = normalized

    @classmethod
    def from_file(cls, path: str | Path) -> "TokenAuthenticator":
        location = Path(path)
        try:
            mode = stat.S_IMODE(location.stat().st_mode)
            if mode & 0o077:
                raise AuthenticationConfigError("Token database must be owner-only; set permissions to 0600")
            payload = json.loads(location.read_text(encoding="utf-8"))
        except AuthenticationConfigError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthenticationConfigError("Could not read token database") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("tokens"), dict):
            raise AuthenticationConfigError("Token database must contain a tokens object")
        return cls(payload["tokens"])

    def authenticate(self, authorization: str | None) -> str | None:
        if not authorization:
            return None
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token or token.strip() != token:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        # Constant-time compare each stored digest. Token lists are expected to
        # stay small for the invite-only deployment.
        matched_user = None
        for stored_digest, user_id in self._token_digests.items():
            if hmac.compare_digest(digest, stored_digest):
                matched_user = user_id
        return matched_user


def add_user_token(path: str | Path, user_id: str) -> str:
    """Issue one high-entropy token and persist only its digest with mode 0600."""
    if not user_id.strip() or len(user_id) > 128:
        raise ValueError("user ID must be between 1 and 128 characters")
    location = Path(path)
    location.parent.mkdir(parents=True, exist_ok=True)
    tokens = _read_token_map(location)
    token = secrets.token_urlsafe(32)
    tokens[hashlib.sha256(token.encode("utf-8")).hexdigest()] = user_id
    _write_token_map(location, tokens)
    return token


def revoke_user_tokens(path: str | Path, user_id: str) -> int:
    """Revoke all credentials for one user and return the number removed."""
    location = Path(path)
    tokens = _read_token_map(location)
    remaining = {digest: owner for digest, owner in tokens.items() if owner != user_id}
    removed = len(tokens) - len(remaining)
    if removed:
        _write_token_map(location, remaining)
    return removed


def _read_token_map(location: Path) -> dict[str, str]:
    if not location.exists():
        return {}
    existing = TokenAuthenticator.from_file(location)
    return dict(existing._token_digests)


def _write_token_map(location: Path, tokens: dict[str, str]) -> None:
    temporary = location.with_name(f".{location.name}.{secrets.token_hex(6)}.tmp")
    try:
        temporary.write_text(json.dumps({"tokens": tokens}, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(location)
        os.chmod(location, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
