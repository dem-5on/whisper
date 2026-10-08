"""Windows logon startup registration for the per-user daemon."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


TASK_NAME = "WhisperTranscriber"


def _daemon_command() -> str:
    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    runner = pythonw if pythonw.is_file() else executable
    return subprocess.list2cmdline([str(runner), "-m", "transcriber.daemon"])


def enable() -> tuple[bool, str]:
    """Register daemon startup at the current user's next logon."""
    if os.name != "nt":
        return False, "Windows startup registration is only available on Windows."
    result = subprocess.run(
        ["schtasks.exe", "/Create", "/F", "/SC", "ONLOGON", "/TN", TASK_NAME, "/TR", _daemon_command()],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        return False, detail or "Task Scheduler could not register Whisper."
    return True, "Whisper will start automatically when you sign in to Windows."


def disable() -> tuple[bool, str]:
    """Remove Whisper's per-user logon task."""
    if os.name != "nt":
        return False, "Windows startup registration is only available on Windows."
    result = subprocess.run(
        ["schtasks.exe", "/Delete", "/F", "/TN", TASK_NAME],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        return False, detail or "Task Scheduler could not remove Whisper's startup task."
    return True, "Whisper will no longer start automatically."
