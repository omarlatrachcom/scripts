#!/bin/zsh

if [[ -n "${1:-}" && -d "$1" ]]; then
  BASE_DIR="${1:A}"
else
  BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
fi

MIN_SECONDS=$((30 * 60))
REPORT="$BASE_DIR/mp3_30mn_or_longer.txt"

format_duration() {
  local total=$1
  local h=$(( total / 3600 ))
  local m=$(( (total % 3600) / 60 ))
  local s=$(( total % 60 ))

  if [ "$h" -gt 0 ]; then
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
done < <(find -s "$BASE_DIR" -maxdepth 1 -type f -iname "*.mp3" -print0)

# Keep the Terminal output visible and save the same listing as a text report.
exec > >(tee "$REPORT")

echo "MP3 files at least 30 minutes long in:"
echo "$BASE_DIR"
echo "Generated: $(date)"
echo

if [ ${#FILES[@]} -eq 0 ]; then
  echo "No MP3 files found."
  echo
  echo "Report saved to:"
  echo "$REPORT"
  echo
  if [[ -t 0 ]]; then
    read -k 1 "?Press any key to close..."
  fi
  exit 0
fi

MATCH_COUNT=0
UNREADABLE_COUNT=0

for FILE in "${FILES[@]}"; do
  DURATION_RAW=$(get_duration "$FILE")

  if [ $? -ne 0 ]; then
    echo "Could not read duration  |  ${FILE:t}"
    UNREADABLE_COUNT=$(( UNREADABLE_COUNT + 1 ))
    continue
  fi

  if awk -v duration="$DURATION_RAW" -v minimum="$MIN_SECONDS" \
    'BEGIN { exit !(duration >= minimum) }'; then
    DURATION_SECONDS=$(awk -v duration="$DURATION_RAW" 'BEGIN { printf "%.0f", duration }')
    echo "$(format_duration "$DURATION_SECONDS")  |  ${FILE:t}"
    MATCH_COUNT=$(( MATCH_COUNT + 1 ))
  fi
done

if [ "$MATCH_COUNT" -eq 0 ]; then
  echo "No MP3 files are 30 minutes or longer."
fi

echo
echo "Matching files: $MATCH_COUNT"
echo "MP3 files scanned: ${#FILES[@]}"
if [ "$UNREADABLE_COUNT" -gt 0 ]; then
  echo "Durations not readable: $UNREADABLE_COUNT"
fi
echo
echo "Report saved to:"
echo "$REPORT"
echo

if [[ -t 0 ]]; then
  read -k 1 "?Press any key to close..."
fi
