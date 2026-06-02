#!/bin/zsh

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
VLC="/Applications/VLC.app/Contents/MacOS/VLC"

if [ ! -x "$VLC" ]; then
  echo "VLC not found at:"
  echo "$VLC"
  echo
  echo "Install VLC in /Applications first."
  read -k 1 "?Press any key to close..."
  exit 1
fi

# Set Mac system volume to 100%
osascript -e "set volume output volume 100"

setopt EXTENDED_GLOB
setopt NULL_GLOB

# Find MP4 files recursively, sorted by path/name
FILES=( "$BASE_DIR"/**/*.(#i)mp4(N) )

if [ ${#FILES[@]} -eq 0 ]; then
  echo "No MP4 files found in:"
  echo "$BASE_DIR"
  read -k 1 "?Press any key to close..."
  exit 0
fi

echo "Playing ${#FILES[@]} MP4 file(s) in order..."
echo

"$VLC" \
  --fullscreen \
  --video-on-top \
  --volume 512 \
  --play-and-exit \
  "${FILES[@]}"

echo
echo "Done."
read -k 1 "?Press any key to close..."