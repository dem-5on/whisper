"""Authenticated WebSocket client for the Whisper hosted-service protocol."""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Callable


_CLOSE = object()


class HostedWhisperEngine:
    """Stream PCM16/16 kHz frames to a user-owned Whisper server."""

    def __init__(self, url: str, token: str, profile: str, language: str | None,
                 publish_partial: Callable[[str, str, int], None],
                 publish_final: Callable[[str, str, int], None],
                 report_metrics: Callable[[float, float, str], None]) -> None:
        self.url = url
        self.token = token
        self.profile = profile
        self.language = language
        self._publish_partial = publish_partial
        self._publish_final = publish_final
        self._report_metrics = report_metrics
        self._audio: queue.Queue[bytes] = queue.Queue(maxsize=1500)
        self._closing = threading.Event()
        self._sender_done = threading.Event()
        self._finish_sent = threading.Event()
        self._sender_error = ""

    def submit_audio(self, pcm_16k: bytes) -> None:
        if not pcm_16k or self._closing.is_set():
            return
        try:
            self._audio.put(bytes(pcm_16k), timeout=0.1)
        except queue.Full as exc:
            raise RuntimeError("Hosted Whisper audio queue is behind; stopping to avoid silent audio loss") from exc

    def commit_turn(self) -> None:
        # The service finalizes the complete recording at session.finish;
        # VAD turn boundaries are only UI signals for this transport.
        return

    def close(self) -> None:
        self._closing.set()

    def run(self, stop: threading.Event, generation: int, session_id: str) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError:
            self._report_metrics(0.0, 0.0, "Install universal-transcriber[realtime] for hosted Whisper")
            return

        started = time.monotonic()
        sender: threading.Thread | None = None
        try:
            with connect(
                self.url,
                additional_headers={"Authorization": f"Bearer {self.token}"},
                open_timeout=15,
                close_timeout=3,
                max_size=64 * 1024,
                compression=None,
            ) as websocket:
                request = {
                    "type": "session.start",
                    "profile": self.profile,
                    "audio": {"encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1},
                }
                if self.language:
                    request["language"] = self.language
                websocket.send(json.dumps(request))
                if not self._await_ready(websocket):
                    return
                sender = threading.Thread(
                    target=self._send_audio, args=(websocket,),
                    name="hosted-whisper-audio", daemon=True,
                )
                sender.start()
                finish_deadline: float | None = None
                received_final = False
                while True:
                    if stop.is_set():
                        self.close()
                    if self._finish_sent.is_set() and finish_deadline is None:
                        finish_deadline = time.monotonic() + 180.0
                    try:
                        raw = websocket.recv(timeout=0.2)
                    except TimeoutError:
                        if finish_deadline is not None and time.monotonic() >= finish_deadline:
                            raise TimeoutError("Hosted Whisper final transcript timed out")
                        if self._sender_error:
                            raise RuntimeError(self._sender_error)
                        continue
                    if not isinstance(raw, str):
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    kind = event.get("type")
                    if kind == "transcript.partial":
                        text = str(event.get("text", ""))
                        self._publish_partial(session_id, text, generation)
                        self._report_metrics(time.monotonic() - started, 0.0, "")
                    elif kind == "transcript.final":
                        received_final = True
                        self._publish_final(session_id, str(event.get("text", "")), generation)
                    elif kind == "session.error":
                        message = str(event.get("message", "Hosted Whisper session failed"))
                        self._report_metrics(0.0, 0.0, message[:160])
                        return
                    elif kind == "session.closed":
                        if not received_final:
                            self._report_metrics(0.0, 0.0, "Hosted Whisper closed without a final transcript")
                        return
        except Exception as exc:
            self._report_metrics(0.0, 0.0, _safe_error(exc))
        finally:
            self.close()
            if sender is not None:
                sender.join(timeout=2.0)

    def _send_audio(self, websocket: object) -> None:
        try:
            while True:
                if self._closing.is_set() and self._audio.empty():
                    websocket.send(json.dumps({"type": "session.finish"}))  # type: ignore[attr-defined]
                    self._finish_sent.set()
                    return
                try:
                    frame = self._audio.get(timeout=0.05)
                except queue.Empty:
                    continue
                websocket.send(frame)  # type: ignore[attr-defined]
        except Exception as exc:
            self._sender_error = _safe_error(exc)
        finally:
            self._sender_done.set()

    @staticmethod
    def _await_ready(websocket: object) -> bool:
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            try:
                raw = websocket.recv(timeout=max(0.1, deadline - time.monotonic()))  # type: ignore[attr-defined]
            except TimeoutError as exc:
                raise TimeoutError("Hosted Whisper session setup timed out") from exc
            if not isinstance(raw, str):
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "session.ready":
                return True
            if event.get("type") == "session.error":
                raise RuntimeError(str(event.get("message", "Hosted Whisper rejected the session")))
        raise TimeoutError("Hosted Whisper session setup timed out")


def hosted_dependency_available() -> bool:
    try:
        from websockets.sync.client import connect as _connect  # noqa: F401
    except ImportError:
        return False
    return True


def _safe_error(exc: Exception) -> str:
    message = str(exc).replace("\n", " ")
    if "Bearer " in message:
        return "Hosted Whisper connection failed"
    return message[:160] or "Hosted Whisper connection failed"
