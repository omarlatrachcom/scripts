#!/bin/zsh

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONUNBUFFERED=1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_NAME="youtube_channel_views_browser.py"
LOG_DIR="$HOME/Library/Logs/scripts-apps"
LOG_FILE="$LOG_DIR/youtube-channel-views.log"
APP_VENV_PYTHON="$HOME/Library/Application Support/YouTubeChannelViewsBrowser/venv/bin/python"

mkdir -p "$LOG_DIR"
cd "$SCRIPT_DIR" || exit 1

if [ -x "$APP_VENV_PYTHON" ]; then
  PYTHON_BIN="$APP_VENV_PYTHON"
else
  PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
fi

if [ -z "$PYTHON_BIN" ]; then
  echo "python3 was not found."
  echo "Install Python 3 first, for example with:"
  echo "  brew install python"
  echo
  read -k 1 "?Press any key to close..."
  exit 1
fi

echo "Opening YouTube Channel Views Browser GUI..."
echo "Script: $SCRIPT_DIR/$SCRIPT_NAME"
echo "Python: $PYTHON_BIN"
echo "Log: $LOG_FILE"
echo

"$PYTHON_BIN" -u "$SCRIPT_DIR/$SCRIPT_NAME" 2>&1 | tee "$LOG_FILE"
STATUS=${pipestatus[1]}

echo
if [ "$STATUS" -eq 0 ]; then
  echo "Done."
else
  echo "Finished with error code $STATUS."
fi

read -k 1 "?Press any key to close..."
