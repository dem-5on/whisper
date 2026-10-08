"""Focused-window delivery through the native Windows input/clipboard APIs."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import time

from ...config import DeliveryConfig
from ...delivery import DeliveryError

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D
VK_CONTROL = 0x11
VK_V = 0x56
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
_WORD = ctypes.c_uint16
_DWORD = ctypes.c_uint32
_LONG = ctypes.c_int32


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", _WORD),
        ("wScan", _WORD),
        ("dwFlags", _DWORD),
        ("time", _DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", _LONG),
        ("dy", _LONG),
        ("mouseData", _DWORD),
        ("dwFlags", _DWORD),
        ("time", _DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _windows_api():
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError) as exc:
        raise DeliveryError("Windows input APIs are unavailable on this platform") from exc
    user32.SendInput.argtypes = (_DWORD, ctypes.POINTER(_INPUT), ctypes.c_int)
    user32.SendInput.restype = _DWORD
    return user32, kernel32


class WindowsKeyboardDelivery:
    """Insert Unicode directly into the currently focused Windows app."""

    TYPE_CHUNK_CHARS = 200

    def __init__(self) -> None:
        self.user32, _ = _windows_api()

    def insert(self, text: str) -> None:
        for offset in range(0, len(text), self.TYPE_CHUNK_CHARS):
            units = text[offset:offset + self.TYPE_CHUNK_CHARS].encode("utf-16-le")
            events = []
            for index in range(0, len(units), 2):
                code_unit = int.from_bytes(units[index:index + 2], "little")
                events.append(_INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(0, code_unit, KEYEVENTF_UNICODE, 0, 0)))
                events.append(_INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(0, code_unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)))
            self._send(events)

    def submit(self) -> None:
        self._send_vk(VK_RETURN)

    def _send_vk(self, key: int) -> None:
        self._send([
            _INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(key, 0, 0, 0, 0)),
            _INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(key, 0, KEYEVENTF_KEYUP, 0, 0)),
        ])

    def _send(self, events: list[_INPUT]) -> None:
        if not events:
            return
        array = (_INPUT * len(events))(*events)
        sent = self.user32.SendInput(len(events), array, ctypes.sizeof(_INPUT))
        if sent != len(events):
            raise DeliveryError(f"Windows could not type into the focused app (error {ctypes.get_last_error()})")


class WindowsClipboardDelivery(WindowsKeyboardDelivery):
    """Copy transcript to the Windows clipboard, then paste into focus."""

    def __init__(self) -> None:
        super().__init__()
        try:
            _, self.kernel32 = _windows_api()
            self.user32.OpenClipboard.argtypes = (wintypes.HWND,)
            self.user32.OpenClipboard.restype = wintypes.BOOL
            self.user32.EmptyClipboard.restype = wintypes.BOOL
            self.user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
            self.user32.SetClipboardData.restype = wintypes.HANDLE
            self.user32.CloseClipboard.restype = wintypes.BOOL
            self.kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
            self.kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
            self.kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
            self.kernel32.GlobalLock.restype = ctypes.c_void_p
            self.kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
            self.kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)
        except (AttributeError, OSError) as exc:
            raise DeliveryError("Windows clipboard APIs are unavailable") from exc

    def insert(self, text: str) -> None:
        payload = (text + "\0").encode("utf-16-le")
        handle = self.kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
        if not handle:
            raise DeliveryError("Could not allocate Windows clipboard memory")
        pointer = self.kernel32.GlobalLock(handle)
        if not pointer:
            self.kernel32.GlobalFree(handle)
            raise DeliveryError("Could not access Windows clipboard memory")
        ctypes.memmove(pointer, payload, len(payload))
        self.kernel32.GlobalUnlock(handle)

        opened = False
        for attempt in range(10):
            if self.user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.05 * (attempt + 1))
        if not opened:
            self.kernel32.GlobalFree(handle)
            raise DeliveryError("Could not open the Windows clipboard")
        try:
            self.user32.EmptyClipboard()
            if not self.user32.SetClipboardData(CF_UNICODETEXT, handle):
                self.kernel32.GlobalFree(handle)
                raise DeliveryError("Could not write to the Windows clipboard")
        finally:
            self.user32.CloseClipboard()
        self._send([
            _INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(VK_CONTROL, 0, 0, 0, 0)),
            _INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(VK_V, 0, 0, 0, 0)),
            _INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(VK_V, 0, KEYEVENTF_KEYUP, 0, 0)),
            _INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0, 0)),
        ])


def make_delivery(config: DeliveryConfig):
    return WindowsKeyboardDelivery() if config.backend == "keyboard" else WindowsClipboardDelivery()
