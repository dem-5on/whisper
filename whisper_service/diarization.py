"""Self-hosted, post-session speaker diarization runtime."""

from __future__ import annotations

from pathlib import Path
import os
import threading
from typing import Any, Callable


MODEL_ID = "pyannote/speaker-diarization-community-1"


class DiarizationError(RuntimeError):
    """A safe error raised when the optional diarization runtime is unavailable."""


class PyannoteDiarizationWorker:
    """Run pyannote on the Wicx service host, isolated from live transcription."""

    def __init__(self, *, device: str = "cpu", pipeline: Any | None = None,
                 pipeline_factory: Callable[..., Any] | None = None) -> None:
        if device not in {"cpu", "cuda"}:
            raise ValueError("diarization device must be cpu or cuda")
        self.device = device
        self._pipeline = pipeline
        self._pipeline_factory = pipeline_factory
        self._load_lock = threading.Lock()
        self._inference_lock = threading.Lock()

    def prepare(self) -> None:
        """Load the gated model before enabling the server endpoint."""
        self._ensure_loaded()

    def diarize(self, audio: str | Path) -> list[dict[str, Any]]:
        """Return exclusive, source-timeline speaker turns for one audio file."""
        path = Path(audio)
        if not path.is_file():
            raise DiarizationError("The meeting recording is unavailable")
        with self._inference_lock:
            try:
                return self._diarize_sync(path)
            except DiarizationError:
                raise
            except Exception as exc:
                raise DiarizationError("Speaker analysis failed on the Wicx service") from exc

    def _ensure_loaded(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        with self._load_lock:
            if self._pipeline is not None:
                return self._pipeline
            factory = self._pipeline_factory
            if factory is None:
                try:
                    from pyannote.audio import Pipeline
                except ImportError as exc:
                    raise DiarizationError(
                        "Install universal-transcriber[diarization] to enable hosted speaker analysis"
                    ) from exc
                factory = Pipeline.from_pretrained
            token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
            if not token:
                raise DiarizationError("Set HF_TOKEN after accepting the pyannote model conditions")
            try:
                pipeline = factory(MODEL_ID, token=token)
                if pipeline is None:
                    raise DiarizationError("The pyannote model could not be loaded")
                import torch

                pipeline.to(torch.device(self.device))
                self._pipeline = pipeline
                return pipeline
            except DiarizationError:
                raise
            except Exception as exc:
                raise DiarizationError("The pyannote model could not be initialized") from exc

    def _diarize_sync(self, audio: Path) -> list[dict[str, Any]]:
        pipeline = self._ensure_loaded()
        output = pipeline(str(audio))
        # Exclusive turns avoid overlapping speaker spans, making alignment to
        # one timestamped transcript segment deterministic. Fall back for
        # compatible pipeline outputs that don't expose the exclusive view.
        annotation = getattr(output, "exclusive_speaker_diarization", None)
        if annotation is None:
            annotation = getattr(output, "speaker_diarization", output)
        turns: list[tuple[float, float, str]] = []
        try:
            iterator = annotation.itertracks(yield_label=True)
            for turn, _track, label in iterator:
                start, end = float(turn.start), float(turn.end)
                speaker = str(label)
                if start >= 0 and end > start and speaker:
                    turns.append((start, end, speaker))
        except Exception as exc:
            raise DiarizationError("The speaker model returned an invalid result") from exc
        turns.sort(key=lambda item: (item[0], item[1], item[2]))
        labels: dict[str, str] = {}
        for _start, _end, label in turns:
            if label not in labels:
                labels[label] = f"speaker_{len(labels) + 1}"
        return [
            {"start_seconds": start, "end_seconds": end, "speaker_id": labels[label]}
            for start, end, label in turns
        ]
