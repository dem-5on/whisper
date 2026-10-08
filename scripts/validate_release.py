#!/usr/bin/env python3
"""Validate a DDMMYY release tag against both project version declarations."""

from datetime import date
import re
import sys
import tomllib
from pathlib import Path


def main(tag: str) -> None:
    match = re.fullmatch(r"([0-9]{2})([0-9]{2})([0-9]{2})", tag)
    if match is None:
        raise SystemExit("Release tags must be six digits in DDMMYY format, e.g. 300826.")
    try:
        release_date = date(2000 + int(match[3]), int(match[2]), int(match[1]))
    except ValueError as exc:
        raise SystemExit(f"Invalid DDMMYY release date: {tag}") from exc

    expected = f"{release_date.year}.{release_date.month}.{release_date.day}"
    project = tomllib.loads(Path("pyproject.toml").read_text())
    runtime_version = Path("transcriber/__init__.py").read_text()
    if project["project"]["version"] != expected or f'__version__ = "{expected}"' not in runtime_version:
        raise SystemExit(
            f"Set pyproject.toml and transcriber/__init__.py to {expected} before tagging {tag}."
        )

    print(f"tag={tag}")
    print(f"display={match[1]}/{match[2]}/{match[3]}")
    print(f"version={expected}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: validate_release.py DDMMYY")
    main(sys.argv[1])
