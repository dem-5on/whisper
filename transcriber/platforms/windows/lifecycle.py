"""Per-user process lifecycle for the Windows Whisper daemon."""

from __future__ import annotations

import subprocess
import sys
import time
from typing import Any

from .ipc import request as pipe_request


def request(command: str, timeout: float = 2.0) -> dict[str, Any]:
    return pipe_request({"command": command}, timeout=timeout)


def is_running() -> bool:
    try:
        return request("status", timeout=0.5).get("ok") == "true"
    except (OSError, EOFError, ValueError):
        return False


def start_daemon(timeout: float = 15.0) -> None:
    if is_running():
        return
    subprocess.Popen(
        [sys.executable, "-m", "transcriber.daemon"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running():
            return
        time.sleep(0.25)
    raise RuntimeError("Whisper daemon did not start. Check the installation and Python environment.")


def stop_daemon(timeout: float = 10.0) -> None:
    if not is_running():
        return
    try:
        request("shutdown")
    except (OSError, EOFError, ValueError) as exc:
        raise RuntimeError("Could not ask the Whisper daemon to stop") from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running():
            return
        time.sleep(0.1)
    raise RuntimeError("Whisper daemon did not stop in time")


def restart_daemon() -> None:
    stop_daemon()
    start_daemon()
