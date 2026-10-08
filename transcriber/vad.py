"""Voice activity detection and utterance endpointing.

The default detector is a dependency-free RMS-energy VAD over 16 kHz mono
16-bit PCM frames, which is good enough for push-to-talk auto-stop and for
gating rolling-window decodes. When ``webrtcvad`` is installed it is used
for per-frame speech classification instead (same interface); Silero-grade
neural VAD remains a future upgrade and is intentionally not required here.
"""

from __future__ import annotations

import math
import struct
import time


def frame_rms_dbfs(pcm: bytes) -> float:
    """Root-mean-square level of a 16-bit PCM frame in dBFS (-inf for silence)."""
    if not pcm:
        return float("-inf")
    count = len(pcm) // 2
    if count == 0:
        return float("-inf")
    fmt = f"<{count}h"
    try:
        samples = struct.unpack(fmt, pcm[: count * 2])
    except struct.error:
        return float("-inf")
    energy = sum(s * s for s in samples) / count
    if energy <= 0:
        return float("-inf")
    rms = math.sqrt(energy) / 32768.0
    if rms <= 0:
        return float("-inf")
    return 20.0 * math.log10(rms)


def sensitivity_to_threshold_dbfs(sensitivity: float) -> float:
    """Map 0.0 (picky) .. 1.0 (sensitive) to an RMS speech threshold in dBFS."""
    clamped = min(1.0, max(0.0, float(sensitivity)))
    # -30 dBFS (sensitive) .. -45 dBFS is backwards: higher sensitivity must
    # accept quieter speech, i.e. a *lower* (more negative) threshold.
    return -25.0 - clamped * 20.0  # -25 dBFS .. -45 dBFS


class EnergyVad:
    """Threshold VAD with a little hysteresis to avoid flutter on boundaries."""

    def __init__(self, sensitivity: float = 0.5, hangover_frames: int = 3) -> None:
        self.sensitivity = min(1.0, max(0.0, float(sensitivity)))
        self.hangover_frames = max(0, int(hangover_frames))
        self._speech_frames = 0
        self._silence_frames = 0

    @property
    def threshold_dbfs(self) -> float:
        return sensitivity_to_threshold_dbfs(self.sensitivity)

    def score_frame(self, pcm: bytes) -> float:
        return frame_rms_dbfs(pcm)

    def is_speech(self, pcm: bytes) -> bool:
        level = self.score_frame(pcm)
        # A 3 dB hysteresis band prevents boundary flutter: speech has to be
        # a little louder to begin than it does to continue.
        threshold = self.threshold_dbfs - 1.5 if self._speech_frames else self.threshold_dbfs + 1.5
        if level >= threshold:
            self._speech_frames += 1
            self._silence_frames = 0
            return True
        if self._silence_frames < self.hangover_frames and self._speech_frames > 0:
            # Hangover: stay in speech briefly through short gaps.
            self._silence_frames += 1
            return True
        self._speech_frames = 0
        self._silence_frames += 1
        return False


def create_vad(sensitivity: float = 0.5):  # type: ignore[no-untyped-def]
    """Prefer WebRTC VAD when installed, else the energy fallback.

    Returns an object with ``is_speech(pcm_16k_mono_20ms) -> bool``.
    WebRTC VAD only accepts 10/20/30 ms frames; other sizes fall back to
    energy scoring even when the package is present.
    """
    try:
        import webrtcvad  # type: ignore[import-not-found]
    except ImportError:
        return EnergyVad(sensitivity)
    aggressiveness = 2 if sensitivity >= 0.66 else (1 if sensitivity >= 0.33 else 0)

    class _WebRtcVad:
        def __init__(self) -> None:
            self._vad = webrtcvad.Vad(aggressiveness)
            self._fallback = EnergyVad(sensitivity)

        def is_speech(self, pcm: bytes) -> bool:
            # 16 kHz mono int16: 10ms=320B. Capture frames can be 20--100
            # ms, so classify their constituent 10 ms frames rather than
            # silently falling back for the default 20/50 ms settings.
            if pcm and len(pcm) % 320 == 0:
                try:
                    return any(
                        self._vad.is_speech(pcm[offset:offset + 320], 16000)
                        for offset in range(0, len(pcm), 320)
                    )
                except Exception:
                    pass
            return self._fallback.is_speech(pcm)

    return _WebRtcVad()


class SpeechEndpoint:
    """Tracks speech_started/ended and trailing-silence auto-stop.

    Auto-stop fires only after speech was observed (leading silence while
    the user reaches for the mic must not cut the recording), then
    ``silence_timeout`` seconds of continuous non-speech.
    """

    def __init__(self, silence_timeout_seconds: float = 1.8) -> None:
        self.silence_timeout = max(0.5, float(silence_timeout_seconds))
        self.had_speech = False
        self.in_speech = False
        self._silence_since: float | None = None

    def update(self, is_speech: bool, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        if is_speech:
            self.had_speech = True
            self._silence_since = None
            if not self.in_speech:
                self.in_speech = True
                return "speech_started"
            return None
        if self.in_speech:
            self.in_speech = False
            self._silence_since = now
            return "speech_ended"
        if self._silence_since is None:
            self._silence_since = now
        return None

    def should_auto_stop(self, now: float | None = None) -> bool:
        if not self.had_speech or self.in_speech:
            return False
        now = time.monotonic() if now is None else now
        if self._silence_since is None:
            return False
        return (now - self._silence_since) >= self.silence_timeout

    def reset(self) -> None:
        self.had_speech = False
        self.in_speech = False
        self._silence_since = None
