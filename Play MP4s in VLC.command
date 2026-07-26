#!/bin/zsh

if [[ -n "${1:-}" && -d "$1" ]]; then
  BASE_DIR="${1:A}"
else
  BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
fi
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

# Preserve the exact filename bytes stored by pCloud. zsh globs can normalize
# accented Unicode names into visually identical paths that do not exist.
FILES=()
while IFS= read -r -d '' DISCOVERED_FILE; do
  FILES+=( "$DISCOVERED_FILE" )
done < <(find -s "$BASE_DIR" -type f -iname "*.mp4" -print0)

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
