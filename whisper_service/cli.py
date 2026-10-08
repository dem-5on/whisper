"""Operator diagnostics for the hosted inference service."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from .auth import add_user_token, revoke_user_tokens
from .hardware import HardwareDetectionError, detect_hardware
from .sessions import ModelProfile, SessionLimits, SessionManager


def main() -> None:
    parser = argparse.ArgumentParser(prog="whisper-server", description="Whisper hosted-service diagnostics")
    subparsers = parser.add_subparsers(dest="command", required=True)
    hardware = subparsers.add_parser("hardware", help="detect the inference device/runtime")
    hardware.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    token = subparsers.add_parser("token", help="manage invite-only user credentials")
    token_commands = token.add_subparsers(dest="token_command", required=True)
    add = token_commands.add_parser("add", help="issue a user token; it is printed once")
    add.add_argument("--user", required=True)
    add.add_argument("--file", type=Path, default=_default_token_file())
    revoke = token_commands.add_parser("revoke", help="revoke all tokens for a user")
    revoke.add_argument("--user", required=True)
    revoke.add_argument("--file", type=Path, default=_default_token_file())
    serve = subparsers.add_parser("serve", help="run the authenticated live WebSocket service")
    serve.add_argument("--host", default="127.0.0.1", help="bind address; use a TLS reverse proxy for public access")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    serve.add_argument("--model", default="base", help="server-owned faster-whisper model profile")
    serve.add_argument("--profile", default="default", help="client-visible allow-listed profile ID")
    serve.add_argument("--token-file", type=Path, default=_default_token_file())
    serve.add_argument("--max-session-seconds", type=int, default=600)
    serve.add_argument("--max-concurrent", type=int, default=2)
    serve.add_argument("--partial-interval", type=float, default=1.0)
    serve.add_argument("--rolling-window", type=int, default=8)
    args = parser.parse_args()
    try:
        if args.command == "hardware":
            print(json.dumps(detect_hardware(args.device).as_dict(), indent=2))
        elif args.command == "token":
            if args.token_command == "add":
                issued = add_user_token(args.file, args.user)
                print(f"Token for {args.user} (copy it now; it cannot be recovered):\n{issued}")
            else:
                removed = revoke_user_tokens(args.file, args.user)
                print(f"Revoked {removed} token(s) for {args.user}.")
        else:
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
            asyncio.run(_run_service(args))
    except (HardwareDetectionError, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from exc


async def _run_service(args: argparse.Namespace) -> None:
    from .auth import TokenAuthenticator
    from .server import serve_websocket
    from .worker import prepare_worker

    authenticator = TokenAuthenticator.from_file(args.token_file)
    profile = ModelProfile(args.profile, args.model, ("cpu", "cuda"))
    selection, worker = await prepare_worker(
        args.device, profile,
        partial_interval_seconds=args.partial_interval,
        rolling_window_seconds=args.rolling_window,
    )
    logging.getLogger("whisper_service").info(
        "Inference ready: device=%s compute_type=%s model=%s reason=%s",
        selection.device, selection.compute_type, args.model, selection.reason,
    )
    limits = SessionLimits(
        max_audio_seconds=args.max_session_seconds,
        max_audio_bytes=args.max_session_seconds * 16_000 * 2,
        max_concurrent_global=args.max_concurrent,
        max_concurrent_per_user=1,
    )
    manager = SessionManager(worker, (profile,), device=selection.device, limits=limits)
    await serve_websocket(authenticator, manager, host=args.host, port=args.port)


def _default_token_file() -> Path:
    override = os.environ.get("WHISPER_TOKENS_FILE")
    if override:
        return Path(override)
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "whisper-server" / "tokens.json"


if __name__ == "__main__":
    main()
