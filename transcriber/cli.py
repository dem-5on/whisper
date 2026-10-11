"""Small stateless Unix-socket client for desktop shortcut bindings."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

from .config import KEY_ENV, write_key
from .daemon import default_socket_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Control the Wicx transcription service")
    parser.add_argument(
        "command",
        choices=("toggle", "cancel", "status", "retry", "retranscribe", "last", "history", "sessions", "get-session", "delete-session", "rename-speaker", "retry-session", "export-session", "export-session-audio", "dictionary", "start-session", "stop-session", "mics", "models", "set-provider", "set-model", "set-live-engine", "set-live-model", "set-mic", "set-key", "set-streaming", "events", "subscribe", "autostart", "shutdown"),
    )
    parser.add_argument("autostart_action", nargs="?", choices=("enable", "disable"), help="enable or disable Windows sign-in startup")
    parser.add_argument("--socket", type=Path, default=default_socket_path())
    parser.add_argument("--provider", choices=("local", "groq", "openrouter", "openai", "hosted-whisper"), default=None)
    parser.add_argument("--live-engine", choices=("local", "openai-realtime", "hosted-whisper"), default=None)
    parser.add_argument("--url", default=None, help="Hosted transcription WebSocket URL (wss://host/v1/live)")
    parser.add_argument("--model", default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument("--enabled", default=None, help="true/false for set-streaming (omit to flip)")
    parser.add_argument("--since", default="0", help="Event revision to stream from (events/subscribe)")
    parser.add_argument("--limit", type=int, default=50, help="Maximum transcript history entries to return")
    parser.add_argument("--offset", type=int, default=0, help="Skip this many older session entries (sessions)")
    parser.add_argument("--search", default=None, help="Find text in saved source transcripts or translations (sessions)")
    parser.add_argument("--session", default=None, help="Session id for events, session detail, rename, retry, or exports")
    parser.add_argument("--speaker-id", default=None, help="Stable detected speaker ID, for example speaker_1")
    parser.add_argument("--name", default=None, help="Display name to assign to that Meeting speaker")
    parser.add_argument("--output", type=Path, default=None, help="Destination file for a session export (must not already exist)")
    parser.add_argument("--action", choices=("list", "add", "remove"), help="Dictionary action")
    parser.add_argument("--term", default=None, help="One custom vocabulary term")
    parser.add_argument("--mode", choices=("lecture", "meeting"), help="Mode for start-session")
    parser.add_argument("--session-mode", choices=("dictation", "lecture", "meeting"), help="Filter saved sessions by mode")
    parser.add_argument("--consent", action="store_true", help="Confirm people being recorded have been informed")
    parser.add_argument("--key", default=None, help="API key value (omit for a hidden prompt)")
    parser.add_argument("--clear", action="store_true", help="Remove the stored key with set-key")
    parser.add_argument("--json", action="store_true", help="Print the full daemon response")
    args = parser.parse_args()
    if args.command == "autostart":
        if args.autostart_action is None:
            parser.error("autostart requires enable or disable")
        from .platforms.windows.startup import disable, enable

        ok, message = (enable if args.autostart_action == "enable" else disable)()
        print(message, file=sys.stdout if ok else sys.stderr)
        raise SystemExit(0 if ok else 1)
    if args.command == "set-key":
        raise SystemExit(_set_key(args))
    if args.command == "subscribe":
        raise SystemExit(_stream_events(args))
    payload: dict[str, str] = {"command": args.command.replace("set-provider", "set_backend").replace("set-model", "set_model").replace("set-live-engine", "set_live_engine").replace("set-live-model", "set_live_model").replace("set-mic", "set_mic").replace("set-streaming", "set_streaming")}
    if args.command == "events":
        payload = {"command": "events", "since": args.since}
        if args.session:
            payload["session_id"] = args.session
    elif args.command in {"history", "sessions"}:
        payload["limit"] = str(args.limit)
        if args.command == "sessions":
            payload["offset"] = str(args.offset)
            if args.session_mode:
                payload["mode"] = args.session_mode
            if args.search is not None:
                payload["search"] = args.search
    elif args.command == "delete-session":
        if not args.session:
            parser.error("delete-session requires --session ID")
        payload["command"] = "delete_session"
        payload["session_id"] = args.session
    elif args.command == "get-session":
        if not args.session:
            parser.error("get-session requires --session ID")
        payload["command"] = "get_session"
        payload["session_id"] = args.session
    elif args.command == "retry-session":
        if not args.session:
            parser.error("retry-session requires --session ID")
        payload["command"] = "retry_session"
        payload["session_id"] = args.session
    elif args.command == "rename-speaker":
        if not args.session or not args.speaker_id or not args.name:
            parser.error("rename-speaker requires --session ID --speaker-id speaker_1 --name NAME")
        payload["command"] = "rename_speaker"
        payload["session_id"] = args.session
        payload["speaker_id"] = args.speaker_id
        payload["name"] = args.name
    elif args.command == "export-session":
        if not args.session:
            parser.error("export-session requires --session ID")
        if args.output is None:
            parser.error("export-session requires --output PATH")
        payload["command"] = "export_session"
        payload["session_id"] = args.session
        payload["output"] = str(args.output.expanduser().resolve())
    elif args.command == "export-session-audio":
        if not args.session:
            parser.error("export-session-audio requires --session ID")
        if args.output is None:
            parser.error("export-session-audio requires --output PATH")
        payload["command"] = "export_session_audio"
        payload["session_id"] = args.session
        payload["output"] = str(args.output.expanduser().resolve())
    elif args.command == "dictionary":
        action = args.action or "list"
        if action in {"add", "remove"} and not args.term:
            parser.error(f"dictionary --action {action} requires --term")
        payload["action"] = action
        if args.term is not None:
            payload["term"] = args.term
    elif args.command == "start-session":
        if not args.mode:
            parser.error("start-session requires --mode lecture|meeting")
        if not args.consent:
            parser.error("start-session requires --consent after informing people who may be recorded")
        payload["command"] = "start_session"
        payload["mode"] = args.mode
        payload["consent_acknowledged"] = True
    elif args.command == "stop-session":
        payload["command"] = "stop_session"
    if payload["command"] == "set_backend":
        if not args.provider:
            print("wicx: --provider is required", file=sys.stderr)
            raise SystemExit(2)
        payload["backend"] = args.provider
    if payload["command"] == "set_model":
        if not args.model:
            print("wicx: --model is required (e.g. tiny, base, small, medium, large-v3, turbo)", file=sys.stderr)
            raise SystemExit(2)
        payload["model"] = args.model
    if payload["command"] == "set_live_engine":
        if not args.live_engine:
            print("wicx: --live-engine is required (local, openai-realtime, or hosted-whisper)", file=sys.stderr)
            raise SystemExit(2)
        payload["engine"] = args.live_engine
        if args.url is not None:
            payload["server_url"] = args.url
    if payload["command"] == "set_live_model":
        if not args.model:
            print("wicx: --model is required", file=sys.stderr)
            raise SystemExit(2)
        payload["model"] = args.model
    if payload["command"] == "set_mic":
        if args.device is None:
            print("wicx: --device is required (use default for the default source)", file=sys.stderr)
            raise SystemExit(2)
        payload["device"] = args.device
    if payload["command"] == "set_streaming" and args.enabled is not None:
        payload["enabled"] = args.enabled
    try:
        response = _request_daemon(args, payload)
    except (OSError, EOFError, json.JSONDecodeError, ValueError):
        print("wicx: daemon unavailable", file=sys.stderr)
        raise SystemExit(1)
    if response.get("ok") != "true":
        print(f"wicx: {response.get('error', 'command failed')} ({response.get('state', 'unknown')})", file=sys.stderr)
        raise SystemExit(1)
    if args.json:
        print(json.dumps(response, indent=2))
    elif args.command == "last":
        print(response.get("transcript", ""))
    elif args.command == "history":
        for item in response.get("history", []):
            print(f"{item.get('created_at', '')}  {item.get('transcript', '')}")
    elif args.command == "sessions":
        for item in response.get("sessions", []):
            transcript = item.get("english_translation") or item.get("source_transcript", "")
            print(f"{item.get('id', '')}  {item.get('created_at', '')}  {item.get('mode', '')}  {item.get('state', '')}  {transcript}")
        if response.get("has_more"):
            next_offset = response.get("offset", args.offset) + response.get("limit", args.limit)
            print(f"More sessions available; continue with --offset {next_offset}")
    elif args.command == "get-session":
        session = response.get("session", {})
        print(session.get("source_transcript", ""))
        for segment in session.get("segments", []):
            speaker = segment.get("speaker_id") or "unassigned"
            print(f"[{segment.get('start_seconds', 0):.3f}-{segment.get('end_seconds', 0):.3f}] {speaker}: {segment.get('text', '')}")
    elif args.command == "retry-session":
        print("Retry started; local session transcription is processing")
    elif args.command == "rename-speaker":
        print(f"Renamed {response.get('speaker_id', 'speaker')} to {response.get('name', '')}")
    elif args.command == "export-session":
        print(response.get("export_path", ""))
    elif args.command == "export-session-audio":
        print(response.get("export_path", ""))
    elif args.command == "dictionary":
        if args.action in {"add", "remove"}:
            print("Dictionary updated" if response.get("changed") else "Dictionary unchanged")
        else:
            for term in response.get("terms", []):
                print(term)
    elif args.command == "mics":
        for mic in response.get("mics", []):
            marker = "*" if mic.get("default") else " "
            print(f"{marker} {mic.get('id')}: {mic.get('name')}")
    elif args.command == "models":
        for model in response.get("models", []):
            marker = "*" if model.get("active") else " "
            print(f"{marker} {model.get('id')}")
        if response.get("cached"):
            print("(cached list)", file=sys.stderr)
    elif args.command == "events":
        print(json.dumps(response.get("events", []), indent=2 if args.json else None))
    elif args.command == "shutdown":
        print("Wicx transcription service is stopping")
    else:
        print(response.get("state", "unknown"))


def _stream_events(args: argparse.Namespace) -> int:
    """Persistent subscription: print live events until interrupted."""
    payload = {"command": "subscribe", "since": args.since}
    if args.session:
        payload["session_id"] = args.session
    try:
        if os.name == "nt":
            from .platforms.windows.ipc import stream

            for event in stream(payload):
                if not event.get("subscribed"):
                    print(json.dumps(event))
                    sys.stdout.flush()
            return 0
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(args.socket))
            client.sendall((json.dumps(payload) + "\n").encode())
            stream = client.makefile("rb")
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("subscribed"):
                    continue
                print(json.dumps(event))
                sys.stdout.flush()
    except OSError as exc:
        print(f"wicx: daemon unavailable ({exc})", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


def _request_daemon(args: argparse.Namespace, payload: dict[str, object]) -> dict[str, object]:
    if os.name == "nt":
        from .platforms.windows.ipc import request

        return request(payload)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(args.socket))
        client.sendall((json.dumps(payload) + "\n").encode())
        return json.loads(client.makefile("rb").readline(65536))


def _set_key(args: argparse.Namespace) -> int:
    """Store or remove a provider API key locally, then reload the daemon."""
    if not args.provider or args.provider == "local":
        print("wicx: --provider is required (groq, openrouter, openai, or hosted-whisper)", file=sys.stderr)
        return 2
    name = KEY_ENV[args.provider]
    if args.clear:
        try:
            write_key(None, name, None)
        except OSError as exc:
            print(f"wicx: could not update keys file: {exc}", file=sys.stderr)
            return 1
        print(f"Removed {name}; restarting daemon")
        return _restart_daemon()
    key = args.key or getpass.getpass(f"{name}: ")
    key = key.strip()
    if not key:
        print("wicx: empty key; nothing stored", file=sys.stderr)
        return 2
    try:
        write_key(None, name, key)
    except OSError as exc:
        print(f"wicx: could not update keys file: {exc}", file=sys.stderr)
        return 1
    print(f"Stored {name} (…{key[-4:]}); restarting daemon")
    return _restart_daemon()


def _restart_daemon() -> int:
    if os.name == "nt":
        from .platforms.windows.lifecycle import restart_daemon

        try:
            restart_daemon()
        except (OSError, RuntimeError) as exc:
            print(f"wicx: could not restart the daemon: {exc}", file=sys.stderr)
            return 1
        return 0
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", "transcriber"],
            check=True, capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        print("wicx: automatic restart failed; run `systemctl --user restart wicx`", file=sys.stderr)
        return 1
    return 0
