#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/dem-5on/whisper"
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DOWNLOAD_DIR=""

if [[ ! -f "$SOURCE_DIR/pyproject.toml" || ! -d "$SOURCE_DIR/extension" ]]; then
    for command in curl tar; do
        command -v "$command" >/dev/null || { echo "Please install $command and try again." >&2; exit 1; }
    done
    DOWNLOAD_DIR="$(mktemp -d)"
    trap 'rm -rf "$DOWNLOAD_DIR"' EXIT
    echo "Downloading Whisper installer and app files..."
    curl -fsSL "$REPO_URL/releases/latest/download/whisper-linux-gnome.tar.gz" \
        | tar -xz -C "$DOWNLOAD_DIR"
    SOURCE_DIR="$DOWNLOAD_DIR"
    bash "$DOWNLOAD_DIR/install.sh" --from-download "$@"
    exit $?
fi

if [[ "${1:-}" == "--from-download" ]]; then
    shift
fi

if [[ "${XDG_SESSION_TYPE:-}" != "wayland" ]]; then
    echo "Whisper's packaged desktop integration currently supports GNOME on Wayland." >&2
    echo "Log into a GNOME Wayland session, then run this installer again." >&2
    exit 1
fi
if [[ "${XDG_CURRENT_DESKTOP:-}:${DESKTOP_SESSION:-}" != *GNOME* ]]; then
    echo "Whisper's packaged desktop integration currently supports GNOME on Wayland." >&2
    echo "Detected desktop: ${XDG_CURRENT_DESKTOP:-unknown}." >&2
    exit 1
fi

if ! command -v apt-get >/dev/null; then
    echo "This installer currently supports Debian/Ubuntu-based Linux only." >&2
    echo "Install Python 3.11+, python3-venv, SoX (rec), and ydotool manually, then run this from the app folder." >&2
    exit 1
fi

if ! command -v sudo >/dev/null; then
    echo "sudo is required to install the system audio and Python packages." >&2
    exit 1
fi

echo "Installing required system packages (you may be asked for your password)..."
sudo apt-get update
sudo apt-get install -y sox ydotool pipewire-bin python3-venv curl

PYTHON=""
for candidate in python3 python3.12 python3.11; do
    if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
        PYTHON="$(command -v "$candidate")"
        break
    fi
done
if [[ -z "$PYTHON" ]]; then
    echo "Whisper requires Python 3.11 or newer. Install it, then re-run this installer." >&2
    exit 1
fi

USER_HOME="$(getent passwd "$(id -u)" | cut -d: -f6)"
DATA_HOME="${XDG_DATA_HOME:-$USER_HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$USER_HOME/.config}"
APP_DIR="$DATA_HOME/whisper"
VENV_DIR="$APP_DIR/venv"
BIN_DIR="$USER_HOME/.local/bin"
EXTENSION_DIR="$USER_HOME/.local/share/gnome-shell/extensions/transcriber@local"
UNIT_DIR="$CONFIG_HOME/systemd/user"

mkdir -p "$APP_DIR" "$BIN_DIR" "$EXTENSION_DIR" "$UNIT_DIR" "$CONFIG_HOME/transcriber"
cp -a "$SOURCE_DIR/transcriber" "$SOURCE_DIR/whisper_service" "$APP_DIR/"
cp "$SOURCE_DIR/pyproject.toml" "$SOURCE_DIR/README.md" "$APP_DIR/"
cp "$SOURCE_DIR/uninstall.sh" "$APP_DIR/uninstall.sh"
if [[ ! -f "$CONFIG_HOME/transcriber/config.yaml" ]]; then
    cp "$SOURCE_DIR/config.example.yaml" "$CONFIG_HOME/transcriber/config.yaml"
fi

"$PYTHON" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install "${APP_DIR}[local,realtime]"
ln -sfn "$VENV_DIR/bin/whisper" "$BIN_DIR/whisper"
ln -sfn "$VENV_DIR/bin/whisper-daemon" "$BIN_DIR/whisper-daemon"
ln -sfn "$VENV_DIR/bin/transcriber" "$BIN_DIR/transcriber"
ln -sfn "$VENV_DIR/bin/transcriber-daemon" "$BIN_DIR/transcriber-daemon"

cp "$SOURCE_DIR/extension/metadata.json" "$SOURCE_DIR/extension/extension.js" \
    "$SOURCE_DIR/extension/stylesheet.css" "$EXTENSION_DIR/"

UNIT_FILE="$UNIT_DIR/transcriber.service"
if [[ -f "$UNIT_FILE" ]] && ! grep -qF "$VENV_DIR/bin/whisper-daemon" "$UNIT_FILE"; then
    cp "$UNIT_FILE" "$UNIT_FILE.backup-$(date +%Y%m%d%H%M%S)"
fi
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Whisper voice transcription daemon
After=graphical-session.target

[Service]
Type=simple
ExecStart=$VENV_DIR/bin/whisper-daemon
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now transcriber.service
if command -v gnome-extensions >/dev/null; then
    gnome-extensions enable transcriber@local || true
fi

echo
echo "Whisper is installed. The local base model downloads the first time it starts."
echo "If GNOME does not show the panel icon yet, sign out and back in once."
echo "Open Extensions and enable 'Transcriber' if it is not enabled already."
echo "Wayland text insertion needs ydotool access to /dev/uinput. If the first recording"
echo "transcribes but doesn't type into the active app, open README.md → Installation help"
echo "for the one-time device permission check."
echo "Run 'whisper status' to check the daemon."
