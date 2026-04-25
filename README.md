# macOS GUI Scripts

This repository contains standalone Python/Tkinter GUI tools for common local
macOS workflows: prompt management, YouTube downloads, subtitle translation, and
video/subtitle splitting.

The scripts are designed to run directly with Python 3, and they can also be
wrapped as clickable macOS apps using Automator.

## General Setup

Install Python 3 with Tkinter. With Homebrew, this is usually:

```bash
brew install python python-tk
```

Some tools need extra command line utilities:

```bash
brew install ffmpeg
```

For the YouTube downloader, install or update the Python packages used by
`yt-dlp`:

```bash
python3 -m pip install -U pip yt-dlp yt-dlp-ejs
```

If you run these from Automator, remember that Automator does not always inherit
your normal terminal PATH. The launcher examples below add common Homebrew paths.

## Scripts

### `prompt_manager_gui_mac_fixed.py`

Prompt Manager is a local GUI for organizing reusable prompts by project.

What it does:

- Stores projects and prompts locally in:
  `~/Library/Application Support/PromptManager/store.json`
- Lets you create, edit, delete, and organize prompts.
- Lets you compose a final prompt with subject/input text.
- Supports different separator styles, including Markdown rules, headings, and
  XML-style tags.
- Includes an attachment hint mode for cases where the real source material is
  attached separately in another app.

Run it from Terminal:

```bash
python3 prompt_manager_gui_mac_fixed.py
```

Use it when you want a small local prompt library instead of repeatedly copying
prompt text from notes.

### `smart_ytdlp_downloader_gui_mac_fixed.py`

Smart YouTube Downloader is a high-contrast GUI wrapper around `yt-dlp`.

What it does:

- Downloads a single YouTube video or a playlist.
- Supports video downloads, audio-only MP3 downloads, and SRT-only subtitle
  downloads.
- Supports playlist start/end ranges.
- Can use browser cookies from Firefox, Chrome, Chromium, Brave, Edge, or Safari.
- Saves GUI state in:
  `~/Library/Application Support/SmartYTDownloader/gui_state.json`
- Checks for `ffmpeg` so media can be merged or converted.
- Cleans common temporary/residue files left behind by interrupted `yt-dlp` runs.
- Tries to auto-update `yt-dlp` and `yt-dlp-ejs` periodically before launching.

Run it from Terminal:

```bash
python3 smart_ytdlp_downloader_gui_mac_fixed.py
```

Recommended setup:

```bash
python3 -m pip install -U yt-dlp yt-dlp-ejs
brew install ffmpeg
```

Optional but useful for modern YouTube extraction:

```bash
brew install node
```

Use it when you want a GUI for repeatable YouTube/playlist downloads without
typing long `yt-dlp` commands.

### `srt_translator_gui_mac.py`

SRT Translator is a subtitle translation helper for creating Arabic subtitles
from English, French, or Spanish `.srt` files.

What it does:

- Finds source subtitles named like:
  `<name>.en.srt`, `<name>.fr.srt`, or `<name>.es.srt`
- Extracts subtitle text and splits it into chunks of 150 text lines.
- Adds a strict translation prompt for each chunk so you can copy it into
  ChatGPT.
- Lets you paste translated chunk output back into the app.
- Rebuilds a synced Arabic subtitle file named:
  `<name>.ar.srt`
- Can create bilingual `.ass` subtitles with Arabic plus the original language.
- Can create Arabic-only `.ass` subtitles.
- Can open the matching video in VLC.

Run it from Terminal:

```bash
python3 srt_translator_gui_mac.py
```

Expected workflow:

1. Put the video and source subtitle in the same folder.
2. Name the subtitle with a supported language suffix, for example
   `movie.en.srt`.
3. Open the app and choose the folder.
4. Load the source SRT.
5. Copy each generated chunk into ChatGPT.
6. Paste each translated chunk back into the matching tab.
7. Rebuild the Arabic SRT or create ASS subtitle outputs.

Optional VLC setup:

```bash
brew install --cask vlc
```

Use it when you want to preserve subtitle timing while translating text manually
through ChatGPT.

### `video_splitter_gui_mac.py`

Video Splitter is a GUI for splitting local video files and, optionally, their
matching subtitles.

What it does:

- Lets you pick a folder and select a video file.
- Supports a two-part cut at a chosen timestamp.
- Supports window splitting with overlap, useful for breaking long videos into
  reviewable chunks.
- Can split one or two SRT files alongside the video.
- Can convert split subtitle outputs to ASS.
- Uses `ffmpeg` and `ffprobe` for video work.
- Saves GUI state in:
  `~/Library/Application Support/VideoSplitGUI/gui_state.json`

Run it from Terminal:

```bash
python3 video_splitter_gui_mac.py
```

Required setup:

```bash
brew install ffmpeg
```

Use it when you need local video segments and want the subtitle timing to follow
the split outputs.

## Turn a Script Into a macOS App With Automator

You can make each script clickable from Finder, Spotlight, or the Dock by
wrapping it in an Automator application.

1. Open Automator.
2. Choose `Application`.
3. Add the `Run Shell Script` action.
4. Set `Shell` to `/bin/bash` or `/bin/zsh`.
5. Set `Pass input` to `to stdin`.
6. Paste one of the launcher scripts below.
7. Save the Automator app, for example as `Prompt Manager.app`.
8. Optional: drag the saved app to the Dock.

If you keep this repository somewhere other than `~/Documents/scripts`, change
the `SCRIPT_DIR` value in the launcher.

### Prompt Manager Automator Launcher

```bash
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SCRIPT_DIR="$HOME/Documents/scripts"
SCRIPT_NAME="prompt_manager_gui_mac_fixed.py"
LOG_DIR="$HOME/Library/Logs/scripts-apps"
mkdir -p "$LOG_DIR"

cd "$SCRIPT_DIR" || exit 1
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
nohup "$PYTHON_BIN" "$SCRIPT_DIR/$SCRIPT_NAME" > "$LOG_DIR/prompt-manager.log" 2>&1 &
```

### Smart YouTube Downloader Automator Launcher

```bash
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SCRIPT_DIR="$HOME/Documents/scripts"
SCRIPT_NAME="smart_ytdlp_downloader_gui_mac_fixed.py"
LOG_DIR="$HOME/Library/Logs/scripts-apps"
mkdir -p "$LOG_DIR"

cd "$SCRIPT_DIR" || exit 1
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
nohup "$PYTHON_BIN" "$SCRIPT_DIR/$SCRIPT_NAME" > "$LOG_DIR/smart-ytdlp-downloader.log" 2>&1 &
```

### SRT Translator Automator Launcher

```bash
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SCRIPT_DIR="$HOME/Documents/scripts"
SCRIPT_NAME="srt_translator_gui_mac.py"
LOG_DIR="$HOME/Library/Logs/scripts-apps"
mkdir -p "$LOG_DIR"

cd "$SCRIPT_DIR" || exit 1
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
nohup "$PYTHON_BIN" "$SCRIPT_DIR/$SCRIPT_NAME" > "$LOG_DIR/srt-translator.log" 2>&1 &
```

### Video Splitter Automator Launcher

```bash
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SCRIPT_DIR="$HOME/Documents/scripts"
SCRIPT_NAME="video_splitter_gui_mac.py"
LOG_DIR="$HOME/Library/Logs/scripts-apps"
mkdir -p "$LOG_DIR"

cd "$SCRIPT_DIR" || exit 1
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
nohup "$PYTHON_BIN" "$SCRIPT_DIR/$SCRIPT_NAME" > "$LOG_DIR/video-splitter.log" 2>&1 &
```

## Troubleshooting Automator Launchers

If an Automator app opens and immediately closes:

- Check the matching log file in `~/Library/Logs/scripts-apps/`.
- Confirm `python3` is installed:
  `python3 --version`
- Confirm Tkinter is available:
  `python3 -m tkinter`
- Confirm tool dependencies are installed:
  `ffmpeg -version`, `ffprobe -version`, or
  `python3 -m pip show yt-dlp`
- If Automator finds the wrong Python, replace the `PYTHON_BIN` line with an
  explicit path, for example:

```bash
PYTHON_BIN="/opt/homebrew/bin/python3"
```

