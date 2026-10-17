"""Meeting post-processing definitions for hosted diarization and analysis.

Pure constants and small helpers; transport and persistence live on the daemon.
"""

from __future__ import annotations


#: Text stages after diarization, in run order.
MEETING_STAGES: tuple[str, ...] = ("translation", "summary", "decisions", "actions")

#: Analysis kind -> session outputs key holding the finished artifact.
MEETING_OUTPUT_KEYS: dict[str, str] = {
    "summary": "meeting_summary",
    "decisions": "meeting_decisions",
    "actions": "action_items",
}

#: Stay comfortably under the service 60k-char request cap.
MEETING_MAX_STAGE_CHARS = 45_000

#: Speaker-labeled transcript for analysis prompts ("Speaker 1 [00:12]: words").
MAX_SPEAKER_TEXT_CHARS = 45_000

#: Diarization polling: every 5s for up to an hour in the worker thread.
DIARIZATION_POLL_SECONDS = 5.0
DIARIZATION_MAX_POLLS = 720

#: Bounded upload: the service accepts 4h; refuse earlier with a clear state.
MAX_DIARIZATION_SECONDS = 4 * 60 * 60

#: Auto-retry budget for failed diarization submissions.
MAX_DIARIZATION_ATTEMPTS = 3


def speaker_labeled_text(segments: list[dict], speaker_names: dict[str, str]) -> str:
    """Render transcript lines prefixed with resolved speaker display names."""
    lines: list[str] = []
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        speaker_id = segment.get("speaker_id")
        if isinstance(speaker_id, str) and speaker_id:
            display = speaker_names.get(speaker_id, speaker_id.replace("_", " ").title())
        else:
            display = "Unknown speaker"
        lines.append(f"{display}: {text}")
    return "\n".join(lines)
