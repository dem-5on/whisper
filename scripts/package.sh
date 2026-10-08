#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$ROOT/pyproject.toml" | head -n 1)"
[[ -n "$VERSION" ]] || { echo "Could not read project version." >&2; exit 1; }

DIST_DIR="$ROOT/dist"
mkdir -p "$DIST_DIR"
ARCHIVE="$DIST_DIR/whisper-$VERSION-linux-gnome.tar.gz"
EXTENSION="$DIST_DIR/transcriber-gnome-extension-$VERSION.zip"

git -C "$ROOT" ls-files --cached --others --exclude-standard -z \
    | tar --create --gzip --file="$ARCHIVE" --directory="$ROOT" --null --files-from=-

python3 - "$ROOT" "$EXTENSION" <<'PY'
from pathlib import Path
import sys
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(sys.argv[1]) / "extension"
destination = Path(sys.argv[2])
with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
    for name in ("metadata.json", "extension.js", "stylesheet.css"):
        archive.write(root / name, f"transcriber@local/{name}")
PY

echo "Created:"
echo "  $ARCHIVE"
echo "  $EXTENSION"
