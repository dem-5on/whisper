"""Per-user Windows locations for Whisper configuration, data, and IPC."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile


def config_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Whisper"


def data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Whisper"


def socket_path() -> Path:
    # Windows AF_UNIX paths have a short maximum; use the user's temp folder.
    return Path(tempfile.gettempdir()) / "whisper-daemon.sock"
