"""Short-lived, one-time OAuth transactions for desktop sign-in.

Provider authorization and token verification are deliberately handled by the
HTTP integration layer. This module owns only CSRF/nonce/PKCE transaction state
and the one-time handoff from a verified browser callback to the desktop app.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import base64
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat


_PROVIDERS = frozenset({"google", "apple"})
_PKCE_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43,128}\Z")
_PKCE_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}\Z")
_STATE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_TRANSACTION_TTL = timedelta(minutes=10)
_HANDOFF_TTL = timedelta(minutes=2)


class OAuthTransactionError(RuntimeError):
    """An OAuth state, callback, or one-time handoff is invalid or expired."""


@dataclass(frozen=True)
class OAuthStart:
    state: str
    nonce: str
    provider_pkce_challenge: str


@dataclass(frozen=True)
class PendingOAuthCallback:
    provider: str
    nonce: str
    pkce_challenge: str
    provider_pkce_verifier: str


class OAuthTransactionStore:
    """Persist hashed state and handoff codes in an owner-only SQLite file.

    Callers must validate the provider's signed ID token, issuer, audience,
    expiry, and nonce before calling ``complete_callback``. This class is not
    itself an OAuth client or a provider-token validator.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prepare_private_file()
        try:
            with closing(self._connect()) as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS oauth_transactions (
                        state_digest TEXT PRIMARY KEY,
                        provider TEXT NOT NULL CHECK (provider IN ('google', 'apple')),
                        nonce TEXT NOT NULL,
                        pkce_challenge TEXT NOT NULL,
                        provider_pkce_verifier TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS oauth_handoffs (
                        code_digest TEXT PRIMARY KEY,
                        account_id TEXT NOT NULL,
                        pkce_challenge TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    );
                    """
                )
                columns = {row[1] for row in connection.execute("PRAGMA table_info(oauth_transactions)")}
                if "provider_pkce_verifier" not in columns:
                    connection.execute(
                        "ALTER TABLE oauth_transactions ADD COLUMN provider_pkce_verifier TEXT NOT NULL DEFAULT ''"
                    )
        except sqlite3.Error as exc:
            raise OAuthTransactionError("Could not initialize OAuth transaction database") from exc

    def begin(self, *, provider: str, pkce_challenge: str,
              now: datetime | None = None) -> OAuthStart:
        """Create short-lived state and nonce bound to the desktop's PKCE challenge."""
        if not isinstance(provider, str) or provider not in _PROVIDERS:
            raise ValueError("identity provider must be google or apple")
        if not isinstance(pkce_challenge, str) or not _PKCE_CHALLENGE.fullmatch(pkce_challenge):
            raise ValueError("PKCE challenge must be a base64url SHA-256 challenge")
        current = _aware_utc(now)
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        provider_verifier = secrets.token_urlsafe(48)
        provider_challenge = _challenge(provider_verifier)
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM oauth_transactions WHERE expires_at <= ?", (_timestamp(current),))
                connection.execute("DELETE FROM oauth_handoffs WHERE expires_at <= ?", (_timestamp(current),))
                connection.execute(
                    "INSERT INTO oauth_transactions VALUES (?, ?, ?, ?, ?, ?)",
                    (_digest(state), provider, nonce, pkce_challenge, provider_verifier,
                     _timestamp(current + _TRANSACTION_TTL)),
                )
                connection.commit()
        except sqlite3.Error as exc:
            raise OAuthTransactionError("Could not create OAuth transaction") from exc
        return OAuthStart(state=state, nonce=nonce, provider_pkce_challenge=provider_challenge)

    def pending_callback(self, *, provider: str, state: str,
                          now: datetime | None = None) -> PendingOAuthCallback:
        """Read a pending transaction so the caller can validate the ID-token nonce."""
        if (not isinstance(provider, str) or provider not in _PROVIDERS or
                not isinstance(state, str) or not _STATE.fullmatch(state)):
            raise OAuthTransactionError("OAuth callback state is invalid")
        current = _aware_utc(now)
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    """SELECT provider, nonce, pkce_challenge, provider_pkce_verifier, expires_at
                       FROM oauth_transactions WHERE state_digest = ?""",
                    (_digest(state),),
                ).fetchone()
        except sqlite3.Error as exc:
            raise OAuthTransactionError("Could not read OAuth transaction") from exc
        if row is None or row[0] != provider or _parse_timestamp(row[4]) <= current:
            raise OAuthTransactionError("OAuth callback state is invalid or expired")
        return PendingOAuthCallback(
            provider=row[0], nonce=row[1], pkce_challenge=row[2], provider_pkce_verifier=row[3],
        )

    def complete_callback(self, *, provider: str, state: str, verified_nonce: str,
                          account_id: str,
                          now: datetime | None = None) -> str:
        """Consume state after ID-token verification and issue a desktop code.

        The integration must validate signature, issuer, audience, expiry, and
        nonce before this call; requiring the verified nonce here binds that
        validation to this exact transaction.
        """
        if (not isinstance(provider, str) or provider not in _PROVIDERS or
                not isinstance(state, str) or not _STATE.fullmatch(state)):
            raise OAuthTransactionError("OAuth callback state is invalid")
        if not isinstance(account_id, str) or not account_id.strip() or len(account_id) > 128:
            raise ValueError("account ID is invalid")
        current = _aware_utc(now)
        code = secrets.token_urlsafe(32)
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """SELECT provider, nonce, pkce_challenge, expires_at
                       FROM oauth_transactions WHERE state_digest = ?""",
                    (_digest(state),),
                ).fetchone()
                if (row is None or row[0] != provider or row[3] <= _timestamp(current) or
                        not isinstance(verified_nonce, str) or
                        not hmac.compare_digest(row[1], verified_nonce)):
                    connection.rollback()
                    raise OAuthTransactionError("OAuth callback state is invalid, expired, or already used")
                connection.execute(
                    "DELETE FROM oauth_transactions WHERE state_digest = ?", (_digest(state),),
                )
                connection.execute(
                    "INSERT INTO oauth_handoffs VALUES (?, ?, ?, ?)",
                    (_digest(code), account_id, row[2], _timestamp(current + _HANDOFF_TTL)),
                )
                connection.commit()
        except OAuthTransactionError:
            raise
        except sqlite3.Error as exc:
            raise OAuthTransactionError("Could not complete OAuth transaction") from exc
        return code

    def redeem_handoff(self, *, code: str, pkce_verifier: str,
                       now: datetime | None = None) -> str:
        """Consume a handoff once, only when the desktop proves its PKCE verifier."""
        if not isinstance(code, str) or not code or len(code) > 256:
            raise OAuthTransactionError("OAuth handoff is invalid")
        if not isinstance(pkce_verifier, str) or not _PKCE_VERIFIER.fullmatch(pkce_verifier):
            raise OAuthTransactionError("OAuth handoff proof is invalid")
        current = _aware_utc(now)
        verifier_challenge = _challenge(pkce_verifier)
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT account_id, pkce_challenge, expires_at FROM oauth_handoffs WHERE code_digest = ?",
                    (_digest(code),),
                ).fetchone()
                if (row is None or _parse_timestamp(row[2]) <= current or
                        not hmac.compare_digest(row[1], verifier_challenge)):
                    connection.rollback()
                    raise OAuthTransactionError("OAuth handoff is invalid or expired")
                connection.execute("DELETE FROM oauth_handoffs WHERE code_digest = ?", (_digest(code),))
                connection.commit()
                return row[0]
        except OAuthTransactionError:
            raise
        except sqlite3.Error as exc:
            raise OAuthTransactionError("Could not redeem OAuth handoff") from exc

    def _prepare_private_file(self) -> None:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                mode = stat.S_IMODE(self.path.stat().st_mode)
            except OSError as exc:
                raise OAuthTransactionError("Could not inspect OAuth database permissions") from exc
            if mode & 0o077:
                raise OAuthTransactionError("OAuth database must be owner-only; set permissions to 0600")
        except OSError as exc:
            raise OAuthTransactionError("Could not create OAuth database") from exc
        else:
            os.close(fd)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)


def _aware_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("OAuth event time must include a timezone")
    return current.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def authorization_url(*, provider: str, client_id: str, redirect_uri: str,
                      transaction: OAuthStart) -> str:
    """Build the provider authorization URL; secrets never enter the desktop."""
    from urllib.parse import urlencode
    from urllib.parse import urlparse

    if provider not in _PROVIDERS:
        raise ValueError("identity provider must be google or apple")
    if not client_id or len(client_id) > 512:
        raise ValueError("provider client ID is required")
    redirect = urlparse(redirect_uri)
    if (redirect.scheme != "https" or not redirect.hostname or redirect.username or
            redirect.password or redirect.fragment or len(redirect_uri) > 2048):
        raise ValueError("provider callback must be an HTTPS URL")
    endpoint = {
        "google": "https://accounts.google.com/o/oauth2/v2/auth",
        "apple": "https://appleid.apple.com/auth/authorize",
    }[provider]
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        # Apple accepts only its `name` and `email` scopes; Google requires
        # `openid` to request an OIDC identity token.
        "scope": "openid email profile" if provider == "google" else "email",
        "state": transaction.state,
        "nonce": transaction.nonce,
        "code_challenge": transaction.provider_pkce_challenge,
        "code_challenge_method": "S256",
    }
    if provider == "apple":
        params["response_mode"] = "form_post"
    return f"{endpoint}?{urlencode(params)}"
