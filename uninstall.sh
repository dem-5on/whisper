#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != "--yes" ]]; then
    echo "This removes the Whisper app, GNOME extension, and user service."
    echo "Your settings, API keys, recordings, and transcripts will be kept."
    read -r -p "Continue? [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }
fi

USER_HOME="$(getent passwd "$(id -u)" | cut -d: -f6)"
DATA_HOME="${XDG_DATA_HOME:-$USER_HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$USER_HOME/.config}"

systemctl --user disable --now transcriber.service 2>/dev/null || true
rm -f "$CONFIG_HOME/systemd/user/transcriber.service"
systemctl --user daemon-reload 2>/dev/null || true
rm -rf "$DATA_HOME/whisper"
rm -rf "$USER_HOME/.local/share/gnome-shell/extensions/transcriber@local"
rm -f "$USER_HOME/.local/bin/whisper" "$USER_HOME/.local/bin/whisper-daemon" \
    "$USER_HOME/.local/bin/transcriber" "$USER_HOME/.local/bin/transcriber-daemon"

echo "Whisper app removed. Settings and personal transcription data were kept."
echo "To erase those too, manually remove:"
echo "  $CONFIG_HOME/transcriber"
echo "  ${XDG_DATA_HOME:-$USER_HOME/.local/share}/transcriber"
