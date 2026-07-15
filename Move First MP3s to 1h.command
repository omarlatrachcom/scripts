#!/bin/zsh

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_DIR="$BASE_DIR/1h"
MAX_SECONDS=$((1 * 60 * 60))

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

duration_to_seconds() {
  local raw="$1"

  awk -v duration="$raw" '
    BEGIN {
      if (duration + 0 > 0) {
        printf "%.0f", duration
      } else {
        exit 1
      }
    }
  '
}

get_duration_seconds() {
  local file="$1"
  local raw=""
  local seconds=""

  raw=$(mdls -raw -name kMDItemDurationSeconds "$file" 2>/dev/null)

  if [[ "$raw" == "(null)" || -z "$raw" ]]; then
    if command -v ffprobe >/dev/null 2>&1; then
      raw=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$file" 2>/dev/null)
    fi
  fi

  if [[ "$raw" == "(null)" || -z "$raw" ]]; then
    return 1
  fi

  seconds=$(duration_to_seconds "$raw") || return 1

  if [[ -z "$seconds" || "$seconds" -le 0 ]]; then
    return 1
  fi

  echo "$seconds"
}

setopt EXTENDED_GLOB
setopt NULL_GLOB
setopt NO_CASE_GLOB

FILES=( "$BASE_DIR"/*.(mp3)(N) )
SELECTED_FILES=()
SELECTED_DURATIONS=()
TOTAL_SECONDS=0
STOPPED_AT=""
STOPPED_DURATION=0
STOPPED_REASON=""

echo "Scanning MP3 files in:"
echo "$BASE_DIR"
echo

mkdir -p "$TARGET_DIR"

if [ ${#FILES[@]} -eq 0 ]; then
  echo "No MP3 files found in the current folder."
  echo
  echo "Created folder:"
  echo "$TARGET_DIR"
  echo
  read -k 1 "?Press any key to close..."
  exit 0
fi

for FILE in "${FILES[@]}"; do
  DURATION_SECONDS=$(get_duration_seconds "$FILE")

  if [ $? -ne 0 ]; then
    STOPPED_AT="$FILE"
    STOPPED_REASON="duration could not be read"
    break
  fi

  NEXT_TOTAL=$(( TOTAL_SECONDS + DURATION_SECONDS ))

  if [ "$NEXT_TOTAL" -le "$MAX_SECONDS" ]; then
    SELECTED_FILES+=( "$FILE" )
    SELECTED_DURATIONS+=( "$DURATION_SECONDS" )
    TOTAL_SECONDS=$NEXT_TOTAL
  else
    STOPPED_AT="$FILE"
    STOPPED_DURATION=$DURATION_SECONDS
    STOPPED_REASON="adding this file would exceed 1h"
    break
  fi
done

if [ ${#SELECTED_FILES[@]} -eq 0 ]; then
  echo "No MP3 files can be moved without exceeding 1h."
  echo

  if [ -n "$STOPPED_AT" ]; then
    echo "First file not moved:"
    echo "${STOPPED_AT:t}"

    if [ "$STOPPED_DURATION" -gt 0 ]; then
      echo "Duration: $(format_duration "$STOPPED_DURATION")"
    else
      echo "Reason: $STOPPED_REASON"
    fi

    echo
  fi

  echo "Created folder:"
  echo "$TARGET_DIR"
  echo
  read -k 1 "?Press any key to close..."
  exit 0
fi

echo "Files selected to move into:"
echo "$TARGET_DIR"
echo

for (( i = 1; i <= ${#SELECTED_FILES[@]}; i++ )); do
  echo "$(format_duration "${SELECTED_DURATIONS[$i]}")  |  ${SELECTED_FILES[$i]:t}"
done

echo
echo "Selected files: ${#SELECTED_FILES[@]}"
echo "Selected total duration: $(format_duration "$TOTAL_SECONDS")"
echo "Maximum allowed duration: $(format_duration "$MAX_SECONDS")"
echo

if [ -n "$STOPPED_AT" ]; then
  echo "Stopped before:"
  echo "${STOPPED_AT:t}"
  echo "Reason: $STOPPED_REASON"

  if [ "$STOPPED_DURATION" -gt 0 ]; then
    echo "Duration: $(format_duration "$STOPPED_DURATION")"
  fi

  echo
fi

MOVED_COUNT=0

for FILE in "${SELECTED_FILES[@]}"; do
  mv -f "$FILE" "$TARGET_DIR/"

  if [ $? -eq 0 ]; then
    echo "Moved: ${FILE:t}"
    MOVED_COUNT=$(( MOVED_COUNT + 1 ))
  else
    echo "Failed to move: ${FILE:t}"
  fi
done

echo
echo "Done. Moved $MOVED_COUNT file(s)."
echo "Moved total duration: $(format_duration "$TOTAL_SECONDS")"
echo

read -k 1 "?Press any key to close..."
