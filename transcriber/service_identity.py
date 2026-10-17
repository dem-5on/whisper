"""Identity Wicx presents to its own hosted service.

Every request to the hosted service -- HTTP for analysis, diarization and
rewrite, plus the live WebSocket handshake -- carries an explicit User-Agent.
Reverse proxies in front of the service (Cloudflare, for one) reject default
library agents outright: ``Python-urllib`` is answered with ``error code:
1010``, which looks exactly like an authorization failure to the caller. Saying
who we are keeps those requests reaching the application.
"""

from __future__ import annotations

import os

from . import __version__
from .config import SERVICE_USER_AGENT_VARIABLE


def service_user_agent() -> str:
    """Return the configured User-Agent, or the versioned default.

    An operator-supplied value is only used when it is safe to put in a header:
    non-empty, bounded, and free of newlines or NULs.
    """
    agent = os.environ.get(SERVICE_USER_AGENT_VARIABLE, "").strip()
    if not agent or len(agent) > 256 or any(char in agent for char in "\r\n\0"):
        return f"Wicx/{__version__}"
    return agent


def service_identity_headers() -> dict[str, str]:
    """Headers identifying Wicx on any hosted-service request."""
    return {"User-Agent": service_user_agent(), "Accept": "application/json"}