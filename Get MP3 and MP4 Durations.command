#!/bin/zsh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ -n "${1:-}" && -d "$1" ]]; then
  BASE_DIR="${1:A}"
else
  CHOSEN_DIR=$(osascript <<'APPLESCRIPT'
try
  set chosenFolder to choose folder with prompt "Choose the folder containing the MP3 and MP4 files:"
  return POSIX path of chosenFolder
on error number -128
  return ""
end try
APPLESCRIPT
)
  BASE_DIR="${CHOSEN_DIR%/}"
fi

if [[ -z "$BASE_DIR" || ! -d "$BASE_DIR" ]]; then
  echo "No folder was selected."
  exit 0
fi

REPORT="$BASE_DIR/mp3_mp4_durations_report.txt"

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

echo "Scanning MP3 and MP4 files in:"
echo "$BASE_DIR"
echo

echo "MP3 and MP4 Duration Report" > "$REPORT"
echo "Folder: $BASE_DIR" >> "$REPORT"
echo "Generated: $(date)" >> "$REPORT"
echo "----------------------------------------" >> "$REPORT"
echo >> "$REPORT"

COUNT=0
TOTAL_SECONDS=0

while IFS= read -r -d '' FILE; do
  if command -v ffprobe >/dev/null 2>&1; then
    DURATION_RAW=$(ffprobe -v error -show_entries format=duration \
      -of default=noprint_wrappers=1:nokey=1 "$FILE" 2>/dev/null)
  else
    DURATION_RAW=$(mdls -raw -name kMDItemDurationSeconds "$FILE" 2>/dev/null)
  fi

  if [[ ! "$DURATION_RAW" =~ '^[0-9]+([.][0-9]+)?$' ]]; then
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
done < <(find "$BASE_DIR" -maxdepth 1 -type f \( -iname "*.mp3" -o -iname "*.mp4" \) -print0)

echo
echo "----------------------------------------" >> "$REPORT"
echo "Total MP3/MP4 files: $COUNT" >> "$REPORT"
echo "Total duration: $(format_duration "$TOTAL_SECONDS")" >> "$REPORT"

echo "Total MP3/MP4 files: $COUNT"
echo "Total duration: $(format_duration "$TOTAL_SECONDS")"
echo
echo "Report saved to:"
echo "$REPORT"
echo

if [[ -t 0 ]]; then
  read -k 1 "?Press any key to close..."
fi
