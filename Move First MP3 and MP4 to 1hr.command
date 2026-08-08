#!/bin/zsh

SCRIPT_DIR="${0:A:h}"
exec "$SCRIPT_DIR/Command Support/move_first_mp3_mp4.zsh" 3600 "1hr" "${1:-}"
