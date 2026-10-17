"""Per-application environment file for the hosted Wicx service.

The service reads plain environment variables, so a deployment has three
supported ways to supply them, in priority order:

1. the real process environment (systemd ``EnvironmentFile=`` is the
   recommended production setup),
2. an env file pointed at by ``WHISPER_ENV_FILE``,
3. ``.env`` in the current working directory.

Rules mirror the desktop client's ``keys.env`` handling: ``KEY=VALUE`` lines
only, ``#`` comments and blank lines ignored, one optional layer of quotes
stripped, keys that are not uppercase identifiers skipped, and **a real
environment variable always wins**. Values in the file therefore fill gaps;
they never override what the operator exported or configured in the unit.

This parser is intentionally a small standalone copy rather than an import
from :mod:`transcriber`, because the hosted service can be deployed without
the desktop client package.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import stat


logger = logging.getLogger("whisper_service.env")

#: Environment variable naming an env file explicitly.
ENV_FILE_VARIABLE = "WHISPER_ENV_FILE"

#: File consulted when ``WHISPER_ENV_FILE`` is unset.
DEFAULT_ENV_FILE = ".env"


def default_env_file() -> Path:
    """Return the env file this process would load from."""
    override = os.environ.get(ENV_FILE_VARIABLE, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.cwd() / DEFAULT_ENV_FILE


def parse_env_text(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines, skipping comments and malformed entries."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        key, _, value = entry.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if not key or not value:
            continue
        if key != key.upper() or not key.replace("_", "").isalnum():
            logger.warning("Ignoring env entry with an unusable key: %r", key)
            continue
        values[key] = value
    return values


def load_env_file(path: str | Path | None = None) -> dict[str, str]:
    """Apply an env file to ``os.environ`` and return what was applied.

    Missing files are not an error: a service started from systemd with an
    ``EnvironmentFile=`` simply has nothing to load. Existing environment
    variables are left alone so explicit operator configuration wins.
    """
    location = Path(path).expanduser() if path is not None else default_env_file()
    try:
        text = location.read_text(encoding="utf-8")
        mode = stat.S_IMODE(location.stat().st_mode)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning("Could not read env file %s (%s)", location, type(exc).__name__)
        return {}
    if mode & 0o077:
        logger.warning(
            "Env file %s is readable beyond its owner (mode %o); run chmod 600 %s",
            location, mode, location,
        )
    applied: dict[str, str] = {}
    for key, value in parse_env_text(text).items():
        if key in os.environ:
            continue
        os.environ[key] = value
        applied[key] = value
    if applied:
        # Names only: values are secrets and must never reach a log.
        logger.info("Loaded %d variable(s) from %s: %s", len(applied), location, ", ".join(sorted(applied)))
    return applied