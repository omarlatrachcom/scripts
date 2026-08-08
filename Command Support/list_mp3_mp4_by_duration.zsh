#!/bin/zsh

# Shared implementation for the MP3/MP4 minimum-duration listing commands.

MIN_SECONDS="${1:-}"
MIN_DURATION_LABEL="${2:-}"
REPORT_FILENAME="${3:-}"
REQUESTED_DIR="${4:-}"

if [[ ! "$MIN_SECONDS" =~ '^[0-9]+$' || "$MIN_SECONDS" -le 0 ||
      -z "$MIN_DURATION_LABEL" || -z "$REPORT_FILENAME" ]]; then
  echo "Usage: ${0:t} MIN_SECONDS DURATION_LABEL REPORT_FILENAME [FOLDER]"
  exit 2
fi

if [[ -n "$REQUESTED_DIR" && -d "$REQUESTED_DIR" ]]; then
  BASE_DIR="${REQUESTED_DIR:A}"
else
  BASE_DIR="${0:A:h}"
fi

REPORT="$BASE_DIR/$REPORT_FILENAME"

pause_if_interactive() {
  if [[ -t 0 ]]; then
    read -k 1 "?Press any key to close..."
  fi
}

format_duration() {
  local total=$1
  local h=$(( total / 3600 ))
  local m=$(( (total % 3600) / 60 ))
  local s=$(( total % 60 ))

  if (( h > 0 )); then
    printf "%dh %02dm %02ds" "$h" "$m" "$s"
  else
    printf "%dm %02ds" "$m" "$s"
  fi
}

get_duration() {
  local file="$1"
  local raw=""
  local attempt=1

  # Retry because cloud-backed files can take a moment to become available.
  while (( attempt <= 10 )); do
    if command -v ffprobe >/dev/null 2>&1; then
      raw=$(ffprobe -v error -show_entries format=duration \
        -of default=noprint_wrappers=1:nokey=1 "$file" 2>/dev/null)
    fi
    [[ "$raw" =~ '^[0-9]+([.][0-9]+)?$' ]] && break
    (( attempt++ ))
    (( attempt <= 10 )) && sleep 2
  done

  if [[ ! "$raw" =~ '^[0-9]+([.][0-9]+)?$' ]]; then
    raw=$(mdls -raw -name kMDItemDurationSeconds "$file" 2>/dev/null)
  fi

  [[ "$raw" =~ '^[0-9]+([.][0-9]+)?$' ]] || return 1
  echo "$raw"
}

FILES=()
while IFS= read -r -d '' DISCOVERED_FILE; do
  FILES+=( "$DISCOVERED_FILE" )
done < <(
  find -s "$BASE_DIR" -maxdepth 1 -type f \
    \( -iname '*.mp3' -o -iname '*.mp4' \) -print0
)

# Keep the Terminal output visible and save the same listing as a text report.
exec > >(tee "$REPORT")

echo "MP3 and MP4 files at least $MIN_DURATION_LABEL long in:"
echo "$BASE_DIR"
echo "Generated: $(date)"
echo

if (( ${#FILES[@]} == 0 )); then
  echo "No MP3 or MP4 files found."
  echo
  echo "Report saved to:"
  echo "$REPORT"
  echo
  pause_if_interactive
  exit 0
fi

MATCH_COUNT=0
UNREADABLE_COUNT=0

for FILE in "${FILES[@]}"; do
  DURATION_RAW=$(get_duration "$FILE")

  if (( $? != 0 )); then
    echo "Could not read duration  |  ${FILE:t}"
    (( UNREADABLE_COUNT++ ))
    continue
  fi

  if awk -v duration="$DURATION_RAW" -v minimum="$MIN_SECONDS" \
    'BEGIN { exit !(duration >= minimum) }'; then
    DURATION_SECONDS=$(awk -v duration="$DURATION_RAW" 'BEGIN { printf "%.0f", duration }')
    echo "$(format_duration "$DURATION_SECONDS")  |  ${FILE:t}"
    (( MATCH_COUNT++ ))
  fi
done

if (( MATCH_COUNT == 0 )); then
  echo "No MP3 or MP4 files are $MIN_DURATION_LABEL or longer."
fi

echo
echo "Matching files: $MATCH_COUNT"
echo "MP3/MP4 files scanned: ${#FILES[@]}"
if (( UNREADABLE_COUNT > 0 )); then
  echo "Durations not readable: $UNREADABLE_COUNT"
fi
echo
echo "Report saved to:"
echo "$REPORT"
echo

pause_if_interactive
