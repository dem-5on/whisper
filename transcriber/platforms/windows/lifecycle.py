"""Per-user process lifecycle for the Windows Whisper daemon."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from typing import Any

from .paths import socket_path


def request(command: str, timeout: float = 2.0) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path()))
        client.sendall((json.dumps({"command": command}) + "\n").encode("utf-8"))
        response = client.makefile("rb").readline(65536)
    return json.loads(response)


def is_running() -> bool:
    try:
        return request("status", timeout=0.5).get("ok") == "true"
    except (OSError, json.JSONDecodeError):
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
    except (OSError, json.JSONDecodeError) as exc:
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
