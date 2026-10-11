"""Human-readable export for locally stored Wicx sessions."""

from __future__ import annotations

import json
from pathlib import Path
from .session_store import StoredSession


def render_session_markdown(session: StoredSession) -> str:
    """Render a session without exposing its local audio path or device data."""
    title = {"dictation": "Dictation", "lecture": "Lecture", "meeting": "Meeting"}.get(
        session.mode, "Wicx session"
    )
    lines = [
        f"# {title}",
        "",
        f"- Recorded: {session.created_at}",
        f"- Status: {session.state}",
        "",
        "## Transcript",
        "",
        session.source_transcript.strip() or "_(No transcript available.)_",
    ]
    if session.english_translation:
        lines.extend(["", "## English translation", "", session.english_translation.strip()])
    if session.outputs:
        lines.extend(["", "## Generated outputs", ""])
        for key, value in session.outputs.items():
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
