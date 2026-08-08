#!/bin/zsh

SCRIPT_DIR="${0:A:h}"
exec "$SCRIPT_DIR/Command Support/list_mp3_mp4_by_duration.zsh" \
  3600 "1hr" "mp3_mp4_1hr_or_longer.txt" "${1:-}"
