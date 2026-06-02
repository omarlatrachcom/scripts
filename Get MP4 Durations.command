#!/bin/zsh

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPORT="$BASE_DIR/mp4_durations_report.txt"

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

echo "Scanning MP4 files in:"
echo "$BASE_DIR"
echo

echo "MP4 Duration Report" > "$REPORT"
echo "Folder: $BASE_DIR" >> "$REPORT"
echo "Generated: $(date)" >> "$REPORT"
echo "----------------------------------------" >> "$REPORT"
echo >> "$REPORT"

COUNT=0
TOTAL_SECONDS=0

while IFS= read -r -d '' FILE; do
  DURATION_RAW=$(mdls -raw -name kMDItemDurationSeconds "$FILE" 2>/dev/null)

  if [[ "$DURATION_RAW" == "(null)" || -z "$DURATION_RAW" ]]; then
    DURATION_TEXT="Unknown duration"
  else
    DURATION_SECONDS=$(printf "%.0f" "$DURATION_RAW")
    DURATION_TEXT=$(format_duration "$DURATION_SECONDS")
    TOTAL_SECONDS=$(( TOTAL_SECONDS + DURATION_SECONDS ))
  fi

  REL_PATH="${FILE#$BASE_DIR/}"

  echo "$DURATION_TEXT  |  $REL_PATH"
  echo "$DURATION_TEXT  |  $REL_PATH" >> "$REPORT"

  COUNT=$(( COUNT + 1 ))
done < <(find "$BASE_DIR" -type f -iname "*.mp4" -print0)

echo
echo "----------------------------------------" >> "$REPORT"
echo "Total MP4 files: $COUNT" >> "$REPORT"
echo "Total duration: $(format_duration "$TOTAL_SECONDS")" >> "$REPORT"

echo "Total MP4 files: $COUNT"
echo "Total duration: $(format_duration "$TOTAL_SECONDS")"
echo
echo "Report saved to:"
echo "$REPORT"
echo

read -k 1 "?Press any key to close..."