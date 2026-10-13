"""Human-readable export for locally stored Wicx sessions."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
from .session_store import StoredSession


def render_session_markdown(session: StoredSession) -> str:
    """Render a session without exposing its local audio path or device data."""
    title = {"dictation": "Dictation", "lecture": "Lecture", "meeting": "Meeting"}.get(
        session.mode, "Wicx session"
    )
    transcript = session.source_transcript.strip() or "_(No transcript available.)_"
    if session.segments:
        speaker_names = session.outputs.get("speaker_names", {})
        rendered_segments: list[str] = []
        for segment in session.segments:
            speaker_id = segment.get("speaker_id")
            speaker_name = speaker_names.get(speaker_id) if isinstance(speaker_names, dict) else None
            speaker = speaker_name or (speaker_id.replace("_", " ").title() if speaker_id else "")
            timestamp = _format_timestamp(segment["start_seconds"])
            label = f"**{speaker}:** " if speaker else ""
            rendered_segments.append(f"- `{timestamp}` {label}{segment['text']}")
        transcript = "\n".join(rendered_segments)
    lines = [
        f"# {title}",
        "",
        f"- Recorded: {session.created_at}",
        f"- Status: {session.state}",
        "",
        "## Transcript",
        "",
        transcript,
    ]
    if session.english_translation:
        lines.extend(["", "## English translation", "", session.english_translation.strip()])
    speaker_names = session.outputs.get("speaker_names")
    if isinstance(speaker_names, dict) and speaker_names:
        lines.extend(["", "## Speakers", ""])
        lines.extend(
            f"- {speaker_id.replace('_', ' ').title()}: {name}"
            for speaker_id, name in speaker_names.items()
        )
    generated_outputs = {key: value for key, value in session.outputs.items() if key != "speaker_names"}
    if generated_outputs:
        lines.extend(["", "## Generated outputs", ""])
        for key, value in generated_outputs.items():
            label = str(key).replace("_", " ").strip().capitalize() or "Output"
            lines.extend([f"### {label}", ""])
            if isinstance(value, list):
                lines.extend([f"- {item}" for item in value] or ["_(No items.)_"])
            elif isinstance(value, (dict, bool, int, float)) or value is None:
                lines.extend(["```json", json.dumps(value, ensure_ascii=False, indent=2), "```"])
            else:
                lines.append(str(value))
            lines.append("")
    if session.error:
        lines.extend(["", "## Processing status", "", session.error])
    return "\n".join(lines).rstrip() + "\n"


def _format_timestamp(seconds: float) -> str:
    total_milliseconds = round(seconds * 1000)
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def export_session_markdown(session: StoredSession, output: str | Path) -> Path:
    """Create a Markdown export, refusing to overwrite an existing file."""
    target = Path(output).expanduser()
    content = render_session_markdown(session)
    try:
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    except FileExistsError:
        raise
    except OSError:
        # If writing failed after exclusive creation, remove only the partial
        # file this call created; never touch a pre-existing export.
        if target.exists():
            try:
                target.unlink()
            except OSError:
                pass
        raise
    return target


def export_session_audio(session: StoredSession, output: str | Path) -> Path:
    """Copy retained session audio, refusing to overwrite an existing file."""
    if session.mode not in {"lecture", "meeting"}:
        raise FileNotFoundError("Audio is not retained for this session")
    source = session.audio_path
    if not source.is_file() or source.stat().st_size <= 44:
        raise FileNotFoundError("The session recording is missing or empty")
    target = Path(output).expanduser()
    try:
        with source.open("rb") as audio, target.open("xb") as exported:
            shutil.copyfileobj(audio, exported, length=1024 * 1024)
    except FileExistsError:
        raise
    except OSError:
        if target.exists():
            try:
                target.unlink()
            except OSError:
                pass
        raise
    return target
