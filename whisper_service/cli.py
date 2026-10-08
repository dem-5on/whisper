"""Operator diagnostics for the hosted inference service."""

from __future__ import annotations

import argparse
import json
import sys

from .hardware import HardwareDetectionError, detect_hardware


def main() -> None:
    parser = argparse.ArgumentParser(prog="whisper-server", description="Whisper hosted-service diagnostics")
    subparsers = parser.add_subparsers(dest="command", required=True)
    hardware = subparsers.add_parser("hardware", help="detect the inference device/runtime")
    hardware.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    args = parser.parse_args()
    try:
        print(json.dumps(detect_hardware(args.device).as_dict(), indent=2))
    except (HardwareDetectionError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
