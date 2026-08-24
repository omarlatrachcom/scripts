#!/bin/zsh

# Non-interactive launcher for Finder, Automator, Launch Services, and Terminal.
# It never relies on shell profile files or an interactive terminal.

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONUNBUFFERED=1
export PIP_DISABLE_PIP_VERSION_CHECK=1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_NAME="article_extractor_gui.py"
APP_SUPPORT_DIR="$HOME/Library/Application Support/ArticleExtractor"
VENV_DIR="$APP_SUPPORT_DIR/venv"
VENV_PYTHON="$VENV_DIR/bin/python"
LOG_DIR="$HOME/Library/Logs/scripts-apps"
LOG_FILE="$LOG_DIR/article-extractor.log"

mkdir -p "$APP_SUPPORT_DIR" "$LOG_DIR"
exec >>"$LOG_FILE" 2>&1

print ""
print "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Article Extractor"
print "Script: $SCRIPT_DIR/$SCRIPT_NAME"

show_error_dialog() {
  local message="$1"
  /usr/bin/osascript - "$message" "$LOG_FILE" <<'APPLESCRIPT' >/dev/null 2>&1
on run argv
    set errorMessage to item 1 of argv
    set logPath to item 2 of argv
    display dialog errorMessage & return & return & "Log: " & logPath with title "Article Extractor" buttons {"OK"} default button "OK" with icon stop
end run
APPLESCRIPT
}

python_has_tk() {
  local candidate="$1"
  [ -x "$candidate" ] || return 1
  "$candidate" -c 'import tkinter; assert tkinter.TkVersion >= 8.6' >/dev/null 2>&1
}

find_tk_python() {
  local candidate
  local candidates=(
    "${PYTHON_BIN:-}"
    "/opt/homebrew/bin/python3"
    "/usr/local/bin/python3"
    "/Library/Frameworks/Python.framework/Versions/Current/bin/python3"
    "/usr/bin/python3"
  )
  candidate="$(command -v python3 2>/dev/null || true)"
  candidates+=("$candidate")
  for candidate in "${candidates[@]}"; do
    if [ -n "$candidate" ] && python_has_tk "$candidate"; then
      print -r -- "$candidate"
      return 0
    fi
  done
  return 1
}

BASE_PYTHON="$(find_tk_python || true)"

if [ -z "$BASE_PYTHON" ]; then
  BREW_BIN=""
  for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -x "$candidate" ]; then
      BREW_BIN="$candidate"
      break
    fi
  done

  if [ -n "$BREW_BIN" ]; then
    print "No Tk-capable Python found. Installing Python and Tk through Homebrew…"
    export HOMEBREW_NO_AUTO_UPDATE=1
    if ! "$BREW_BIN" install python python-tk; then
      show_error_dialog "Python/Tk installation failed. Article Extractor could not start."
      exit 1
    fi
    BASE_PYTHON="$(find_tk_python || true)"
  fi
fi

if [ -z "$BASE_PYTHON" ]; then
  show_error_dialog "A Tk-capable Python was not found, and Homebrew is not installed. Install Homebrew once from brew.sh, then open Article Extractor again."
  exit 1
fi

print "Base Python: $BASE_PYTHON"

# Rebuild only this app's private environment if its interpreter disappeared or
# no longer has Tk (which can happen after a Homebrew Python upgrade).
if ! python_has_tk "$VENV_PYTHON"; then
  if [ -d "$VENV_DIR" ]; then
    BACKUP_VENV="$APP_SUPPORT_DIR/venv-incompatible-$(date '+%Y%m%d-%H%M%S')"
    print "Moving incompatible private environment to: $BACKUP_VENV"
    mv "$VENV_DIR" "$BACKUP_VENV"
  fi
  print "Creating private Python environment…"
  if ! "$BASE_PYTHON" -m venv "$VENV_DIR"; then
    show_error_dialog "The private Python environment could not be created. Article Extractor could not start."
    exit 1
  fi
fi

# Retry dependency setup on later launches if a previous attempt was offline.
# Extraction can still use its conservative built-in counter without tiktoken,
# while TelQuel pages specifically require curl-cffi's browser transport.
if ! "$VENV_PYTHON" -c 'import tiktoken, curl_cffi' >/dev/null 2>&1; then
  print "Installing Article Extractor dependencies in the private environment…"
  if ! "$VENV_PYTHON" -m pip install --quiet -r "$SCRIPT_DIR/requirements.txt"; then
    print "Warning: dependency installation failed. The safe UTF-8 token counter remains available, but TelQuel extraction requires curl-cffi."
  fi
fi

if ! python_has_tk "$VENV_PYTHON"; then
  show_error_dialog "Tkinter is unavailable in the private environment. Article Extractor could not start."
  exit 1
fi

print "App Python: $VENV_PYTHON"
if [ "${ARTICLE_EXTRACTOR_BOOTSTRAP_ONLY:-0}" = "1" ]; then
  print "Bootstrap-only check completed successfully."
  exit 0
fi

cd "$SCRIPT_DIR" || exit 1
"$VENV_PYTHON" -u "$SCRIPT_DIR/$SCRIPT_NAME"
STATUS=$?

if [ "$STATUS" -ne 0 ]; then
  show_error_dialog "Article Extractor stopped with error code $STATUS."
fi
exit "$STATUS"
