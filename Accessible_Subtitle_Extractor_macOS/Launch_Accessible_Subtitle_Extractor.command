#!/bin/zsh
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_SCRIPT="$SCRIPT_DIR/Accessible_Subtitle_Extractor.py"

show_error() {
  /usr/bin/osascript -e "display alert \"Accessible Subtitle Extractor\" message \"$1\" as critical" >/dev/null 2>&1 || true
}

if [[ ! -f "$PYTHON_SCRIPT" ]]; then
  show_error "Accessible_Subtitle_Extractor.py must remain in the same folder as this launcher."
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  show_error "Python 3.10 or newer is required. Install Python 3 for macOS, then open this launcher again."
  exit 1
fi

exec python3 "$PYTHON_SCRIPT"
