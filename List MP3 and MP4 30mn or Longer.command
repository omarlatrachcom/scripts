#!/bin/zsh

SCRIPT_DIR="${0:A:h}"
exec "$SCRIPT_DIR/Command Support/list_mp3_mp4_by_duration.zsh" \
  1800 "30mn" "mp3_mp4_30mn_or_longer.txt" "${1:-}"
