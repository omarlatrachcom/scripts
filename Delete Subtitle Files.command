#!/bin/zsh

# Use a folder supplied by a portable launcher, otherwise this script's folder.
if [[ -n "${1:-}" && -d "$1" ]]; then
  BASE_DIR="${1:A}"
else
  BASE_DIR="${0:A:h}"
fi

# Only subtitle-specific extensions belong here. Ambiguous extensions such as
# .txt, .xml, .stl, and .cap are intentionally excluded to protect other data.
SUBTITLE_EXTENSIONS=(
  srt
  vtt webvtt
  ass ssa
  sub idx sup
  sbv
  smi sami
  ttml dfxp itt
  scc usf
  lrc
  mks
)

pause_if_interactive() {
  if [[ -t 0 ]]; then
    read -k 1 "?Press any key to close..."
  fi
}

FIND_EXTENSION_ARGS=()
for EXTENSION in "${SUBTITLE_EXTENSIONS[@]}"; do
  if (( ${#FIND_EXTENSION_ARGS[@]} > 0 )); then
    FIND_EXTENSION_ARGS+=( -o )
  fi
  FIND_EXTENSION_ARGS+=( -iname "*.$EXTENSION" )
done

SUBTITLE_FILES=()
while IFS= read -r -d '' SUBTITLE_FILE; do
  SUBTITLE_FILES+=( "$SUBTITLE_FILE" )
done < <(
  find "$BASE_DIR" -type f \( "${FIND_EXTENSION_ARGS[@]}" \) -print0
)

echo "Searching for subtitle files in:"
echo "$BASE_DIR"
echo

if (( ${#SUBTITLE_FILES[@]} == 0 )); then
  echo "No supported subtitle files found."
  echo
  pause_if_interactive
  exit 0
fi

echo "Found ${#SUBTITLE_FILES[@]} subtitle file(s):"
echo
for SUBTITLE_FILE in "${SUBTITLE_FILES[@]}"; do
  echo "- ${SUBTITLE_FILE#$BASE_DIR/}"
done

echo
read "CONFIRM?Delete these subtitle files permanently? Type YES to continue: "

if [[ "$CONFIRM" != "YES" ]]; then
  echo
  echo "Cancelled. Nothing was deleted."
  echo
  pause_if_interactive
  exit 0
fi

DELETED_COUNT=0
FAILED_COUNT=0

for SUBTITLE_FILE in "${SUBTITLE_FILES[@]}"; do
  if rm -f "$SUBTITLE_FILE"; then
    (( DELETED_COUNT++ ))
  else
    echo "Could not delete: ${SUBTITLE_FILE#$BASE_DIR/}"
    (( FAILED_COUNT++ ))
  fi
done

echo
echo "Done. Deleted $DELETED_COUNT subtitle file(s)."
if (( FAILED_COUNT > 0 )); then
  echo "Failed to delete: $FAILED_COUNT"
fi
echo

pause_if_interactive
exit $(( FAILED_COUNT > 0 ? 1 : 0 ))
