"""Runtime hardware detection for hosted inference workers.

Detection is deliberately conservative: a CUDA worker is selected only when
CTranslate2 is installed, at least one CUDA device is visible, and CTranslate2
reports a supported CUDA compute type. This checks runtime usability, not
whether a particular model fits in GPU memory; model loading remains the final
validation for a selected profile.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


DeviceMode = Literal["auto", "cpu", "cuda"]


class HardwareDetectionError(RuntimeError):
    """A requested inference device cannot be used by this installation."""


@dataclass(frozen=True)
class HardwareSelection:
    requested: DeviceMode
    device: Literal["cpu", "cuda"]
    compute_type: str
    cuda_device_count: int
    cuda_compute_types: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["cuda_compute_types"] = list(self.cuda_compute_types)
        return result


def detect_hardware(mode: str = "auto", *, ctranslate2_module: Any | None = None) -> HardwareSelection:
    """Select a usable CPU or CUDA runtime, with optional CUDA auto-fallback.

    ``cpu`` needs no optional runtime import. ``cuda`` is strict and raises if
    the CUDA runtime cannot be used. ``auto`` falls back to CPU and preserves
    a safe diagnostic reason for the operator.
    """
    if mode not in {"auto", "cpu", "cuda"}:
        raise ValueError("device mode must be one of: auto, cpu, cuda")

    requested: DeviceMode = mode  # validated above
    if mode == "cpu":
        return HardwareSelection(requested, "cpu", "int8", 0, (), "CPU explicitly selected")

    try:
        runtime = ctranslate2_module or _load_ctranslate2()
        count = int(runtime.get_cuda_device_count())
        compute_types = tuple(sorted(str(value) for value in runtime.get_supported_compute_types("cuda"))) if count else ()
        if count < 1:
            raise HardwareDetectionError("No visible CUDA devices")
        if not compute_types:
            raise HardwareDetectionError("CTranslate2 reports no supported CUDA compute types")
        compute_type = _preferred_compute_type(compute_types, device="cuda")
        return HardwareSelection(requested, "cuda", compute_type, count, compute_types,
                                 f"CUDA runtime available ({count} device(s))")
    except Exception as exc:
        reason = _safe_reason(exc)
        if mode == "cuda":
            raise HardwareDetectionError(f"CUDA was requested but is unavailable: {reason}") from exc
        cpu_types = _cpu_compute_types(ctranslate2_module)
        return HardwareSelection(requested, "cpu", _preferred_compute_type(cpu_types, device="cpu"),
                                 0, (), f"CUDA unavailable; using CPU ({reason})")


def _load_ctranslate2() -> Any:
    try:
        import ctranslate2
    except ImportError as exc:
        raise HardwareDetectionError("CTranslate2 is not installed") from exc
    return ctranslate2


def _cpu_compute_types(runtime: Any | None) -> tuple[str, ...]:
    if runtime is None:
        try:
            runtime = _load_ctranslate2()
        except HardwareDetectionError:
            return ("int8", "float32")
    try:
        return tuple(sorted(str(value) for value in runtime.get_supported_compute_types("cpu")))
    except Exception:
        return ("int8", "float32")


def _preferred_compute_type(available: tuple[str, ...], *, device: str) -> str:
    preference = ("float16", "int8_float16", "int8", "float32") if device == "cuda" else ("int8", "int8_float32", "float32")
    return next((kind for kind in preference if kind in available), available[0] if available else "float32")


def _safe_reason(exc: Exception) -> str:
    # Do not leak arbitrary runtime details or environment values in health output.
    if isinstance(exc, HardwareDetectionError):
        return str(exc)
    return type(exc).__name__
