"""Small Windows notification-area controller for the Whisper daemon."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from .lifecycle import request, start_daemon


def _icon_image() -> Any:
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((3, 3, 61, 61), fill="#2878f0")
    draw.rounded_rectangle((27, 12, 37, 38), radius=5, fill="white")
    draw.arc((18, 24, 46, 49), start=0, end=180, fill="white", width=4)
    draw.line((32, 48, 32, 54), fill="white", width=4)
    draw.line((25, 55, 39, 55), fill="white", width=4)
    return image


def main() -> None:
    if os.name != "nt":
        raise SystemExit("The Whisper tray controller is available on Windows only.")
    try:
        import pystray
    except ImportError as exc:
        raise SystemExit("Install Whisper with the Windows extra to use its tray controller.") from exc

    try:
        start_daemon()
    except (OSError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None

    icon: Any = None
    stopped = threading.Event()

    def run_command(command: str) -> None:
        try:
            response = request(command)
            if icon is not None:
                state = response.get("state", "unknown").replace("_", " ").title()
                icon.notify(f"Whisper: {state}", "Whisper")
        except (OSError, EOFError, ValueError):
            if icon is not None:
                icon.notify("The Whisper daemon is unavailable.", "Whisper")

    def toggle(_icon: Any, _item: Any) -> None:
        run_command("toggle")

    def cancel(_icon: Any, _item: Any) -> None:
        run_command("cancel")

    def quit_tray(_icon: Any, _item: Any) -> None:
        run_command("shutdown")
        stopped.set()
        if hotkey_thread_id[0]:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(hotkey_thread_id[0], 0x0012, 0, 0)  # WM_QUIT
        _icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Start / stop recording", toggle, default=True),
        pystray.MenuItem("Cancel recording", cancel),
        pystray.MenuItem("Shortcut: Ctrl+Alt+R", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit Whisper tray", quit_tray),
    )
    icon = pystray.Icon("Whisper", _icon_image(), "Whisper", menu)

    hotkey_thread_id = [0]

    def register_hotkey() -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        hotkey_thread_id[0] = kernel32.GetCurrentThreadId()
        registered = user32.RegisterHotKey(None, 1, 0x0002 | 0x0008, ord("R"))  # MOD_CONTROL | MOD_ALT
        if not registered:
            if icon is not None:
                icon.notify("Ctrl+Alt+R is already in use by another app.", "Whisper")
            return
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message == 0x0312 and message.wParam == 1:  # WM_HOTKEY
                run_command("toggle")
        user32.UnregisterHotKey(None, 1)

    def update_tooltip() -> None:
        while not stopped.is_set():
            try:
                status = request("status", timeout=1.0)
                icon.title = f"Whisper — {status.get('state', 'unknown').replace('_', ' ').title()}"
            except (OSError, EOFError, ValueError):
                icon.title = "Whisper — daemon unavailable"
            time.sleep(2)

    threading.Thread(target=update_tooltip, name="whisper-tray-status", daemon=True).start()
    threading.Thread(target=register_hotkey, name="whisper-global-hotkey", daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
