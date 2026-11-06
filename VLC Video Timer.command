#!/bin/zsh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
exec python3 "$SCRIPT_DIR/vlc_video_timer.py"
