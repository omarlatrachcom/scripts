#!/bin/zsh

# Standalone automatic 1-hour mover. It contains its complete implementation
# and does not call another command or a support script.

MAX_SECONDS=3600
MAX_DURATION_LABEL="1hr"
EXTRA_ITEM_STRICT_LIMIT=0

if [[ -n "${1:-}" && -d "$1" ]]; then
  BASE_DIR="${1:A}"
else
  BASE_DIR="${0:A:h}"
fi

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

  # ffprobe reads cloud-backed files directly when Spotlight metadata is absent.
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
  [[ -n "$seconds" && "$seconds" -gt 0 ]] || return 1

  echo "$seconds"
}

LAST_BATCH_NUMBER=0
while IFS= read -r -d '' EXISTING_BATCH_DIR; do
  BATCH_BASENAME="${EXISTING_BATCH_DIR:t}"
  if [[ "$BATCH_BASENAME" =~ '^p([0-9]+)$' ]]; then
    BATCH_NUMBER=$(( 10#${match[1]} ))
    (( BATCH_NUMBER > LAST_BATCH_NUMBER )) && LAST_BATCH_NUMBER=$BATCH_NUMBER
  fi
done < <(find "$BASE_DIR" -mindepth 1 -maxdepth 1 -type d -name 'p[0-9]*' -print0)

NEXT_BATCH_NUMBER=$(( LAST_BATCH_NUMBER + 1 ))
printf -v BATCH_NAME 'p%02d' "$NEXT_BATCH_NUMBER"
TARGET_DIR="$BASE_DIR/$BATCH_NAME"

setopt NULL_GLOB
setopt NUMERIC_GLOB_SORT

# Use zsh's numeric glob sorting so 2 comes before 5, 9, and 10. All regular
# files are inspected in that order; MP3/MP4/MKV files determine the duration cap,
# while numbered non-media files between them travel with the batch.
ALL_FILES=( "$BASE_DIR"/*(N.) )
MEDIA_FILE_COUNT=0
for DISCOVERED_FILE in "${ALL_FILES[@]}"; do
  FILE_EXTENSION="${DISCOVERED_FILE##*.}"
  FILE_EXTENSION="${FILE_EXTENSION:l}"
  if [[ "$FILE_EXTENSION" == "mp3" || "$FILE_EXTENSION" == "mp4" || "$FILE_EXTENSION" == "mkv" ]]; then
    (( MEDIA_FILE_COUNT++ ))
  fi
done

FILES_TO_MOVE=()
SELECTED_MEDIA_FILES=()
SELECTED_DURATIONS=()
ADDITIONAL_FILES=()
PENDING_NUMBERED_FILES=()
SELECTED_TOTAL_SECONDS=0
STOPPED_AT=""
STOPPED_DURATION=0
READ_ERROR_FILE=""
EXTRA_ITEM_INCLUDED=""
STOPPED_AFTER_EXTRA=0

echo "Scanning MP3, MP4, and MKV files in:"
echo "$BASE_DIR"
echo

mkdir -p "$TARGET_DIR" || {
  echo "Could not create:"
  echo "$TARGET_DIR"
  pause_if_interactive
  exit 1
}

if (( MEDIA_FILE_COUNT == 0 )); then
  echo "No MP3, MP4, or MKV files found in the current folder."
  echo
  echo "Created folder:"
  echo "$TARGET_DIR"
  echo
  pause_if_interactive
  exit 0
fi

for FILE in "${ALL_FILES[@]}"; do
  FILE_EXTENSION="${FILE##*.}"
  FILE_EXTENSION="${FILE_EXTENSION:l}"

  if [[ "$FILE_EXTENSION" != "mp3" && "$FILE_EXTENSION" != "mp4" && "$FILE_EXTENSION" != "mkv" ]]; then
    # Numbered lesson resources are part of the ordered sequence. Unnumbered
    # utility files and generated reports are deliberately left in place.
    if [[ "${FILE:t}" =~ '^[0-9]+' ]]; then
      PENDING_NUMBERED_FILES+=( "$FILE" )
    fi
    continue
  fi

  if [[ -n "$EXTRA_ITEM_INCLUDED" ]]; then
    # The one extra media slot has already been used. Keep the numbered
    # resources that follow it, stopping only when the next media item begins.
    STOPPED_AT="$FILE"
    STOPPED_AFTER_EXTRA=1
    STOPPED_NUMBER=""
    if [[ "${FILE:t}" =~ '^([0-9]+)' ]]; then
      STOPPED_NUMBER=$(( 10#${match[1]} ))
    fi

    for RELATED_FILE in "${PENDING_NUMBERED_FILES[@]}"; do
      RELATED_NUMBER=""
      if [[ "${RELATED_FILE:t}" =~ '^([0-9]+)' ]]; then
        RELATED_NUMBER=$(( 10#${match[1]} ))
      fi
      if [[ -n "$STOPPED_NUMBER" && -n "$RELATED_NUMBER" &&
            "$RELATED_NUMBER" -ge "$STOPPED_NUMBER" ]]; then
        continue
      fi
      FILES_TO_MOVE+=( "$RELATED_FILE" )
      ADDITIONAL_FILES+=( "$RELATED_FILE" )
    done
    PENDING_NUMBERED_FILES=()
    break
  fi

  DURATION_SECONDS=$(get_duration_seconds "$FILE")

  if (( $? != 0 )); then
    READ_ERROR_FILE="$FILE"
    break
  fi

  NEXT_TOTAL=$(( SELECTED_TOTAL_SECONDS + DURATION_SECONDS ))
  if (( NEXT_TOTAL <= MAX_SECONDS )); then
    for RELATED_FILE in "${PENDING_NUMBERED_FILES[@]}"; do
      FILES_TO_MOVE+=( "$RELATED_FILE" )
      ADDITIONAL_FILES+=( "$RELATED_FILE" )
    done
    PENDING_NUMBERED_FILES=()
    FILES_TO_MOVE+=( "$FILE" )
    SELECTED_MEDIA_FILES+=( "$FILE" )
    SELECTED_DURATIONS+=( "$DURATION_SECONDS" )
    SELECTED_TOTAL_SECONDS=$NEXT_TOTAL
  elif (( EXTRA_ITEM_STRICT_LIMIT > 0 && NEXT_TOTAL < EXTRA_ITEM_STRICT_LIMIT )); then
    # This variant may take exactly one boundary-crossing item, provided the
    # resulting total remains strictly below its secondary limit.
    for RELATED_FILE in "${PENDING_NUMBERED_FILES[@]}"; do
      FILES_TO_MOVE+=( "$RELATED_FILE" )
      ADDITIONAL_FILES+=( "$RELATED_FILE" )
    done
    PENDING_NUMBERED_FILES=()
    FILES_TO_MOVE+=( "$FILE" )
    SELECTED_MEDIA_FILES+=( "$FILE" )
    SELECTED_DURATIONS+=( "$DURATION_SECONDS" )
    SELECTED_TOTAL_SECONDS=$NEXT_TOTAL
    EXTRA_ITEM_INCLUDED="$FILE"
    continue
  else
    STOPPED_AT="$FILE"
    STOPPED_DURATION=$DURATION_SECONDS

    # Move numbered resources that fall strictly before the rejected media's
    # sequence number. Resources belonging to the rejected item stay behind.
    if (( ${#SELECTED_MEDIA_FILES[@]} > 0 )); then
      STOPPED_NUMBER=""
      if [[ "${FILE:t}" =~ '^([0-9]+)' ]]; then
        STOPPED_NUMBER=$(( 10#${match[1]} ))
      fi

      for RELATED_FILE in "${PENDING_NUMBERED_FILES[@]}"; do
        RELATED_NUMBER=""
        if [[ "${RELATED_FILE:t}" =~ '^([0-9]+)' ]]; then
          RELATED_NUMBER=$(( 10#${match[1]} ))
        fi
        if [[ -n "$STOPPED_NUMBER" && -n "$RELATED_NUMBER" &&
              "$RELATED_NUMBER" -ge "$STOPPED_NUMBER" ]]; then
          continue
        fi
        FILES_TO_MOVE+=( "$RELATED_FILE" )
        ADDITIONAL_FILES+=( "$RELATED_FILE" )
      done
    fi
    break
  fi
done

# If every media file fit, include any numbered lesson resources after the last
# media file, but continue to leave unnumbered utility files untouched.
if [[ -z "$READ_ERROR_FILE" && -z "$STOPPED_AT" &&
      ${#SELECTED_MEDIA_FILES[@]} -gt 0 ]]; then
  for RELATED_FILE in "${PENDING_NUMBERED_FILES[@]}"; do
    FILES_TO_MOVE+=( "$RELATED_FILE" )
    ADDITIONAL_FILES+=( "$RELATED_FILE" )
  done
fi

# Keep "Move First" strictly sequential: an unreadable earlier file prevents
# later files from being considered or moved.
if [[ -n "$READ_ERROR_FILE" ]]; then
  echo "Nothing was moved."
  echo
  echo "Could not read the duration after 10 attempts:"
  echo "${READ_ERROR_FILE:t}"
  echo
  echo "The sequence stopped at this file; later files were not considered."
  echo "Wait for the cloud service to make it available, then run again."
  echo
  pause_if_interactive
  exit 1
fi

if (( ${#SELECTED_MEDIA_FILES[@]} == 0 )); then
  echo "No MP3, MP4, or MKV files can be moved without exceeding $MAX_DURATION_LABEL."
  echo

  if [[ -n "$STOPPED_AT" ]]; then
    echo "First file not moved:"
    echo "${STOPPED_AT:t}"
    echo "Duration: $(format_duration "$STOPPED_DURATION")"
    echo
  fi

  echo "Created folder:"
  echo "$TARGET_DIR"
  echo
  pause_if_interactive
  exit 0
fi

echo "Media selected to move into:"
echo "$TARGET_DIR"
echo

for (( i = 1; i <= ${#SELECTED_MEDIA_FILES[@]}; i++ )); do
  echo "$(format_duration "${SELECTED_DURATIONS[$i]}")  |  ${SELECTED_MEDIA_FILES[$i]:t}"
done

if (( ${#ADDITIONAL_FILES[@]} > 0 )); then
  echo
  echo "In-sequence non-media files included:"
  for RELATED_FILE in "${ADDITIONAL_FILES[@]}"; do
    echo "- ${RELATED_FILE:t}"
  done
fi

echo
echo "Selected media files: ${#SELECTED_MEDIA_FILES[@]}"
echo "Included non-media files: ${#ADDITIONAL_FILES[@]}"
echo "Total files to move: ${#FILES_TO_MOVE[@]}"
echo "Selected total duration: $(format_duration "$SELECTED_TOTAL_SECONDS")"
echo "Maximum allowed duration: $(format_duration "$MAX_SECONDS")"
if (( EXTRA_ITEM_STRICT_LIMIT > 0 )); then
  echo "One extra item allowed only below: $(format_duration "$EXTRA_ITEM_STRICT_LIMIT")"
fi
echo

if [[ -n "$EXTRA_ITEM_INCLUDED" ]]; then
  echo "Included the first item that crossed $MAX_DURATION_LABEL:"
  echo "${EXTRA_ITEM_INCLUDED:t}"
  echo "The resulting total remains strictly below $(format_duration "$EXTRA_ITEM_STRICT_LIMIT")."
  echo
fi

if [[ -n "$STOPPED_AT" ]]; then
  echo "Stopped before:"
  echo "${STOPPED_AT:t}"
  if (( STOPPED_AFTER_EXTRA > 0 )); then
    echo "Reason: the one extra media item has already been included"
  else
    echo "Reason: adding this file would exceed $MAX_DURATION_LABEL"
    echo "Duration: $(format_duration "$STOPPED_DURATION")"
  fi
  echo
fi

MOVED_COUNT=0
for FILE in "${FILES_TO_MOVE[@]}"; do
  if mv "$FILE" "$TARGET_DIR/"; then
    echo "Moved: ${FILE:t}"
    (( MOVED_COUNT++ ))
  else
    echo "Failed to move: ${FILE:t}"
  fi
done

echo
echo "Done. Moved $MOVED_COUNT file(s)."
echo "Moved this run: $(format_duration "$SELECTED_TOTAL_SECONDS")"
echo "Total duration in $BATCH_NAME: $(format_duration "$SELECTED_TOTAL_SECONDS")"
echo

REMAINING_MEDIA_COUNT=0
for REMAINING_FILE in "$BASE_DIR"/*(N.); do
  REMAINING_EXTENSION="${REMAINING_FILE##*.}"
  REMAINING_EXTENSION="${REMAINING_EXTENSION:l}"
  if [[ "$REMAINING_EXTENSION" == "mp3" || "$REMAINING_EXTENSION" == "mp4" || "$REMAINING_EXTENSION" == "mkv" ]]; then
    (( REMAINING_MEDIA_COUNT++ ))
  fi
done

if (( REMAINING_MEDIA_COUNT > 0 )); then
  if (( REMAINING_MEDIA_COUNT < MEDIA_FILE_COUNT )); then
    echo "Continuing automatically with the next batch..."
    echo
    exec "$0" "$BASE_DIR"
  fi

  echo "Automatic batching stopped because no media file could be moved."
  echo "Remaining media files: $REMAINING_MEDIA_COUNT"
  echo
  pause_if_interactive
  exit 1
fi

echo "Finished. The current folder has no remaining MP3, MP4, or MKV files."
echo

pause_if_interactive
