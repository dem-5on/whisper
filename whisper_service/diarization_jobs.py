"""Bounded, owner-scoped background diarization jobs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import logging
from pathlib import Path
import tempfile
from typing import Any, Protocol
from uuid import uuid4

from .sessions import ServiceError


logger = logging.getLogger("whisper_service.diarization")


class Diarizer(Protocol):
    def diarize(self, audio: str | Path) -> list[dict[str, Any]]: ...


@dataclass
class DiarizationJob:
    job_id: str
    user_id: str
    audio_path: Path
    state: str = "queued"
    turns: list[dict[str, Any]] | None = None


class DiarizationJobManager:
    """Keep uploaded audio only while inference is running; bound queued work."""

    def __init__(self, worker: Diarizer, *, temp_dir: Path | None = None,
                 max_jobs: int = 4, workers: int = 1) -> None:
        if max_jobs < 1 or workers < 1 or workers > max_jobs:
            raise ValueError("job and worker limits must be positive and workers cannot exceed max_jobs")
        self.worker = worker
        self.temp_dir = temp_dir
        self.max_jobs = max_jobs
        self.worker_count = workers
        self.jobs: dict[str, DiarizationJob] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max_jobs)
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        if not self._tasks:
            self._tasks = [asyncio.create_task(self._run(), name=f"diarization-worker-{i}")
                           for i in range(self.worker_count)]

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for job in self.jobs.values():
            job.audio_path.unlink(missing_ok=True)

    async def submit(self, user_id: str, audio_path: Path) -> str:
        async with self._lock:
            active = sum(job.state in {"queued", "processing"} for job in self.jobs.values())
            user_active = sum(job.user_id == user_id and job.state in {"queued", "processing"}
                              for job in self.jobs.values())
            if active >= self.max_jobs or user_active >= 1:
                raise ServiceError("service_busy", "Speaker analysis is busy. Try again shortly.", retryable=True)
            # Keep the result cache bounded too. Old terminal jobs expire
            # opportunistically as new submissions arrive.
            if len(self.jobs) >= self.max_jobs * 100:
                for old_id, old_job in tuple(self.jobs.items()):
                    if old_job.state in {"complete", "failed"}:
                        self.jobs.pop(old_id, None)
                        break
            job_id = uuid4().hex
            job = DiarizationJob(job_id, user_id, audio_path)
            self.jobs[job_id] = job
            try:
                self.queue.put_nowait(job_id)
            except asyncio.QueueFull:
                self.jobs.pop(job_id, None)
                raise ServiceError("service_busy", "Speaker analysis is busy. Try again shortly.", retryable=True)
            return job_id

    async def get(self, user_id: str, job_id: str) -> dict[str, Any] | None:
        async with self._lock:
            job = self.jobs.get(job_id)
            if job is None or job.user_id != user_id:
                return None
            result: dict[str, Any] = {"job_id": job.job_id, "state": job.state}
            if job.state == "complete":
                result["turns"] = job.turns or []
            if job.state == "failed":
                result["error"] = "Speaker analysis failed. You can retry this job."
            return result

    async def _run(self) -> None:
        while True:
            job_id = await self.queue.get()
            job = self.jobs.get(job_id)
            try:
                if job is None:
                    continue
                job.state = "processing"
                try:
                    # Inference is synchronous; keep it off the event loop so
                    # uploads and status polling remain responsive.
                    if inspect.iscoroutinefunction(self.worker.diarize):
                        turns = await self.worker.diarize(job.audio_path)
                    else:
                        turns = await asyncio.to_thread(self.worker.diarize, job.audio_path)
                    job.turns = turns
                    job.state = "complete"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Diarization job failed (%s)", type(exc).__name__)
                    job.state = "failed"
            finally:
                if job is not None:
                    job.audio_path.unlink(missing_ok=True)
                self.queue.task_done()


def create_audio_tempfile(directory: Path | None = None) -> tuple[int, Path]:
    """Create a private temporary audio file for streamed HTTP upload bytes."""
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="wicx-diarization-", suffix=".wav", dir=directory)
    return fd, Path(name)
