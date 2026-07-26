#!/bin/zsh

if [[ -n "${1:-}" && -d "$1" ]]; then
  BASE_DIR="${1:A}"
else
  BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
fi
MAX_SECONDS=$((2 * 60 * 60))

LAST_BATCH_NUMBER=0
while IFS= read -r -d '' EXISTING_BATCH_DIR; do
  BATCH_BASENAME="${EXISTING_BATCH_DIR:t}"
  if [[ "$BATCH_BASENAME" =~ '^p([0-9]+)$' ]]; then
    BATCH_NUMBER=$(( 10#${match[1]} ))
    (( BATCH_NUMBER > LAST_BATCH_NUMBER )) && LAST_BATCH_NUMBER=$BATCH_NUMBER
  fi
done < <(find "$BASE_DIR" -mindepth 1 -maxdepth 1 -type d -name "p[0-9]*" -print0)

NEXT_BATCH_NUMBER=$(( LAST_BATCH_NUMBER + 1 ))
printf -v BATCH_NAME "p%02d" "$NEXT_BATCH_NUMBER"
TARGET_DIR="$BASE_DIR/$BATCH_NAME"

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
  local attempt=1

  # ffprobe reads media files directly and works on pCloud, where Spotlight's
  # mdls commonly returns a "could not find" sentence instead of a duration.
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
  seconds=$(duration_to_seconds "$raw") || return 1

  if [[ -z "$seconds" || "$seconds" -le 0 ]]; then
    return 1
  fi

  echo "$seconds"
}

setopt EXTENDED_GLOB
setopt NULL_GLOB
setopt NO_CASE_GLOB

FILES=()
while IFS= read -r -d '' DISCOVERED_FILE; do
  FILES+=( "$DISCOVERED_FILE" )
done < <(
  find -s "$BASE_DIR" -maxdepth 1 -type f \
    \( -iname "*.mp4" -o -iname "*.m4v" -o -iname "*.mov" -o -iname "*.mkv" \
       -o -iname "*.avi" -o -iname "*.webm" \) -print0
)
SELECTED_FILES=()
SELECTED_DURATIONS=()
TOTAL_SECONDS=0
SELECTED_TOTAL_SECONDS=0
STOPPED_AT=""
STOPPED_DURATION=0
STOPPED_REASON=""
READ_ERROR_FILE=""

echo "Scanning video files in:"
echo "$BASE_DIR"
echo

mkdir -p "$TARGET_DIR"

if [ ${#FILES[@]} -eq 0 ]; then
  echo "No video files found in the current folder."
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
    READ_ERROR_FILE="$FILE"
    break
  fi

  NEXT_TOTAL=$(( TOTAL_SECONDS + DURATION_SECONDS ))

  if [ "$NEXT_TOTAL" -le "$MAX_SECONDS" ]; then
    SELECTED_FILES+=( "$FILE" )
    SELECTED_DURATIONS+=( "$DURATION_SECONDS" )
    TOTAL_SECONDS=$NEXT_TOTAL
    SELECTED_TOTAL_SECONDS=$(( SELECTED_TOTAL_SECONDS + DURATION_SECONDS ))
  else
    STOPPED_AT="$FILE"
    STOPPED_DURATION=$DURATION_SECONDS
    STOPPED_REASON="adding this file would exceed 2h"
    break
  fi
done

# "Move First" is strictly sequential. Do not perform a partial or
# out-of-sequence move when any earlier file cannot be inspected.
if [ -n "$READ_ERROR_FILE" ]; then
  echo "Nothing was moved."
  echo
  echo "Could not read the duration after 10 attempts:"
  echo "${READ_ERROR_FILE:t}"
  echo
  echo "The sequence was stopped at this file. Later files were not considered."
  echo "Wait for pCloud to finish making the file available, then run again."
  echo
  if [[ -t 0 ]]; then
    read -k 1 "?Press any key to close..."
  fi
  exit 1
fi

if [ ${#SELECTED_FILES[@]} -eq 0 ]; then
  echo "No videos can be moved without exceeding 2h."
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
echo "Selected total duration: $(format_duration "$SELECTED_TOTAL_SECONDS")"
echo "Total planned for $BATCH_NAME: $(format_duration "$TOTAL_SECONDS")"
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
echo "Moved this run: $(format_duration "$SELECTED_TOTAL_SECONDS")"
echo "Total duration in $BATCH_NAME: $(format_duration "$TOTAL_SECONDS")"
echo

read -k 1 "?Press any key to close..."
