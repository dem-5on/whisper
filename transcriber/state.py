from __future__ import annotations

from enum import StrEnum


class State(StrEnum):
    IDLE = "IDLE"
    RECORDING = "RECORDING"
    PROCESSING = "PROCESSING"
    DELIVERING = "DELIVERING"
    ERROR = "ERROR"
