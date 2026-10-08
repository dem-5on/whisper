"""Focused-application text delivery with X11 and Wayland kept at the edge."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Protocol

from .config import DeliveryConfig

LOG = logging.getLogger("transcriber.delivery")


class DeliveryError(RuntimeError):
    pass


class DeliveryBackend(Protocol):
    def insert(self, text: str) -> None: ...
    def submit(self) -> None: ...


class KeyboardDelivery:
    # One `ydotool type` call per chunk: a single call for a long dictation
    # can outlast the per-call timeout and die mid-word, cutting text off.
    TYPE_CHUNK_CHARS = 200

    def __init__(self) -> None:
        self.wayland = _detect_wayland()
        self.command = "ydotool" if self.wayland else "xdotool"
        if not shutil.which(self.command):
            hint = "install ydotool and configure /dev/uinput access" if self.wayland else "install xdotool"
            raise DeliveryError(f"Keyboard delivery unavailable: {hint}")

    def insert(self, text: str) -> None:
        command = ["ydotool", "type", "--"] if self.wayland else ["xdotool", "type", "--clearmodifiers", "--delay", "1", "--"]
        for index in range(0, len(text), self.TYPE_CHUNK_CHARS):
            self._run([*command, text[index:index + self.TYPE_CHUNK_CHARS]])

    def submit(self) -> None:
        command = ["ydotool", "key", "28:1", "28:0"] if self.wayland else ["xdotool", "key", "--clearmodifiers", "Return"]
        self._run(command)

    @staticmethod
    def _run(command: list[str]) -> None:
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode(errors="replace").strip()[:200]
            raise DeliveryError(f"Keyboard delivery failed{': ' + detail if detail else ''}") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeliveryError("Keyboard delivery failed") from exc


class ClipboardDelivery:
    def __init__(self) -> None:
        self.wayland = os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))
        self.copy_command = ["wl-copy"] if self.wayland else ["xclip", "-selection", "clipboard"]
        if not shutil.which(self.copy_command[0]):
            raise DeliveryError(f"Clipboard delivery unavailable: install {self.copy_command[0]}")
        self.keyboard = KeyboardDelivery()

    def insert(self, text: str) -> None:
        try:
            subprocess.run(self.copy_command, input=text.encode(), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise DeliveryError("Could not copy transcript to clipboard") from exc
        # Pasting is the insert operation; this intentionally does not submit.
        self.keyboard._run(["ydotool", "key", "29:1", "47:1", "47:0", "29:0"] if self.wayland else ["xdotool", "key", "--clearmodifiers", "ctrl+v"])

    def submit(self) -> None:
        self.keyboard.submit()


def _detect_wayland() -> bool:
    """True when the desktop session is Wayland, even for a daemon started
    before the session environment was imported (no XDG_SESSION_TYPE /
    WAYLAND_DISPLAY in its own env). The compositor socket existing in the
    runtime dir is the fallback signal; plain X11 sessions have no such
    socket, so xdotool stays the choice there."""
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return True
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    display = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
    return os.path.exists(os.path.join(runtime, display))


def make_delivery(config: DeliveryConfig) -> DeliveryBackend:
    if os.name == "nt":
        from .platforms.windows.delivery import make_delivery as make_windows_delivery

        return make_windows_delivery(config)
    return KeyboardDelivery() if config.backend == "keyboard" else ClipboardDelivery()
