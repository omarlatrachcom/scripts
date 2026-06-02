#!/bin/zsh

# Folder where this script is located
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Searching for .srt files in:"
echo "$BASE_DIR"
echo

COUNT=$(find "$BASE_DIR" -type f -iname "*.srt" | wc -l | tr -d ' ')

if [ "$COUNT" = "0" ]; then
  echo "No .srt files found."
  read -k 1 "?Press any key to close..."
  exit 0
fi

echo "Found $COUNT .srt file(s)."
echo
read "CONFIRM?Delete them permanently? Type YES to continue: "

if [ "$CONFIRM" = "YES" ]; then
  find "$BASE_DIR" -type f -iname "*.srt" -delete
  echo
  echo "Done. Deleted $COUNT .srt file(s)."
else
  echo
  echo "Cancelled."
fi

echo
read -k 1 "?Press any key to close..."