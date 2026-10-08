"""Small stateless Unix-socket client for desktop shortcut bindings."""

from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
import socket
import subprocess
import sys

from .config import KEY_ENV, write_key
from .daemon import default_socket_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Control the Whisper transcription daemon")
    parser.add_argument(
        "command",
        choices=("toggle", "cancel", "status", "retry", "retranscribe", "last", "mics", "models", "set-provider", "set-model", "set-live-engine", "set-live-model", "set-mic", "set-key", "set-streaming", "events", "subscribe", "autostart"),
    )
    parser.add_argument("autostart_action", nargs="?", choices=("enable", "disable"), help="enable or disable Windows sign-in startup")
    parser.add_argument("--socket", type=Path, default=default_socket_path())
    parser.add_argument("--provider", choices=("local", "groq", "openrouter", "openai", "hosted-whisper"), default=None)
    parser.add_argument("--live-engine", choices=("local", "openai-realtime", "hosted-whisper"), default=None)
    parser.add_argument("--url", default=None, help="Hosted Whisper WebSocket URL (wss://host/v1/live)")
    parser.add_argument("--model", default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument("--enabled", default=None, help="true/false for set-streaming (omit to flip)")
    parser.add_argument("--since", default="0", help="Event revision to stream from (events/subscribe)")
    parser.add_argument("--session", default=None, help="Filter events to one live session id")
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
    if payload["command"] == "set_backend":
        if not args.provider:
            print("whisper: --provider is required", file=sys.stderr)
            raise SystemExit(2)
        payload["backend"] = args.provider
    if payload["command"] == "set_model":
        if not args.model:
            print("whisper: --model is required (e.g. tiny, base, small, medium, large-v3, turbo)", file=sys.stderr)
            raise SystemExit(2)
        payload["model"] = args.model
    if payload["command"] == "set_live_engine":
        if not args.live_engine:
            print("whisper: --live-engine is required (local, openai-realtime, or hosted-whisper)", file=sys.stderr)
            raise SystemExit(2)
        payload["engine"] = args.live_engine
        if args.url is not None:
            payload["server_url"] = args.url
    if payload["command"] == "set_live_model":
        if not args.model:
            print("whisper: --model is required", file=sys.stderr)
            raise SystemExit(2)
        payload["model"] = args.model
    if payload["command"] == "set_mic":
        if args.device is None:
            print("whisper: --device is required (use default for the default source)", file=sys.stderr)
            raise SystemExit(2)
        payload["device"] = args.device
    if payload["command"] == "set_streaming" and args.enabled is not None:
        payload["enabled"] = args.enabled
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(args.socket))
            client.sendall((json.dumps(payload) + "\n").encode())
            response = json.loads(client.makefile("rb").readline(65536))
    except (OSError, json.JSONDecodeError):
        print("whisper: daemon unavailable", file=sys.stderr)
        raise SystemExit(1)
    if response.get("ok") != "true":
        print(f"whisper: {response.get('error', 'command failed')} ({response.get('state', 'unknown')})", file=sys.stderr)
        raise SystemExit(1)
    if args.json:
        print(json.dumps(response, indent=2))
    elif args.command == "last":
        print(response.get("transcript", ""))
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
    else:
        print(response.get("state", "unknown"))


def _stream_events(args: argparse.Namespace) -> int:
    """Persistent subscription: print live events until interrupted."""
    payload = {"command": "subscribe", "since": args.since}
    if args.session:
        payload["session_id"] = args.session
    try:
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
        print(f"whisper: daemon unavailable ({exc})", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


def _set_key(args: argparse.Namespace) -> int:
    """Store or remove a provider API key locally, then reload the daemon."""
    if not args.provider or args.provider == "local":
        print("whisper: --provider is required (groq, openrouter, openai, or hosted-whisper)", file=sys.stderr)
        return 2
    name = KEY_ENV[args.provider]
    if args.clear:
        try:
            write_key(None, name, None)
        except OSError as exc:
            print(f"whisper: could not update keys file: {exc}", file=sys.stderr)
            return 1
        print(f"Removed {name}; restarting daemon")
        return _restart_daemon()
    key = args.key or getpass.getpass(f"{name}: ")
    key = key.strip()
    if not key:
        print("whisper: empty key; nothing stored", file=sys.stderr)
        return 2
    try:
        write_key(None, name, key)
    except OSError as exc:
        print(f"whisper: could not update keys file: {exc}", file=sys.stderr)
        return 1
    print(f"Stored {name} (…{key[-4:]}); restarting daemon")
    return _restart_daemon()


def _restart_daemon() -> int:
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", "transcriber"],
            check=True, capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        print("whisper: automatic restart failed; run `systemctl --user restart transcriber`", file=sys.stderr)
        return 1
    return 0
