#!/usr/bin/env python3
"""
Smart YouTube Downloader (macOS) - high contrast edition

Main goals:
- keep the downloader behavior
- make the interface readable
- especially make the Download button readable
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, scrolledtext, ttk

UPDATE_INTERVAL_HOURS = 24
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "SmartYTDownloader"
UPDATE_STATE_FILE = APP_SUPPORT_DIR / "last_update.json"
APP_STATE_FILE = APP_SUPPORT_DIR / "gui_state.json"
RESTARTED_AFTER_UPDATE_ENV = "SMART_YTDLP_RESTARTED_AFTER_UPDATE"
UPDATER_PACKAGES = ["yt-dlp", "yt-dlp-ejs"]
SUPPORTED_BROWSERS = ("firefox", "chrome", "chromium", "brave", "edge", "safari")
STARTUP_MESSAGES: list[str] = []


def startup_log(message: str = "") -> None:
    STARTUP_MESSAGES.append(message)


def in_virtualenv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix) or hasattr(sys, "real_prefix")


def ensure_state_dir() -> None:
    APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)


def read_last_update_ts() -> float | None:
    try:
        data = json.loads(UPDATE_STATE_FILE.read_text(encoding="utf-8"))
        ts = data.get("last_update_ts")
        if isinstance(ts, (int, float)):
            return float(ts)
    except Exception:
        return None
    return None


def write_last_update_ts(ts: float) -> None:
    ensure_state_dir()
    UPDATE_STATE_FILE.write_text(json.dumps({"last_update_ts": ts}, indent=2), encoding="utf-8")


def should_run_update() -> bool:
    last = read_last_update_ts()
    if last is None:
        return True
    return (time.time() - last) >= UPDATE_INTERVAL_HOURS * 3600


def pip_upgrade_commands() -> list[list[str]]:
    base = [sys.executable, "-m", "pip", "install", "-U"]
    if in_virtualenv():
        return [base + UPDATER_PACKAGES]
    return [
        base + ["--user"] + UPDATER_PACKAGES,
        base + UPDATER_PACKAGES,
    ]


def run_auto_update_before_import() -> None:
    if os.environ.get(RESTARTED_AFTER_UPDATE_ENV) == "1":
        startup_log("> App restarted after package auto-update. Using the refreshed yt-dlp installation.")
        return
    if not should_run_update():
        startup_log("> Package auto-update check skipped (last successful check was recent enough).")
        return

    startup_log("> Checking/updating yt-dlp packages before starting...")

    updated_successfully = False
    last_error: str | None = None

    for cmd in pip_upgrade_commands():
        startup_log("> Running:")
        startup_log(f"  {' '.join(cmd)}")
        proc = subprocess.run(cmd, text=True, capture_output=True)
        if proc.returncode == 0:
            updated_successfully = True
            if proc.stdout.strip():
                startup_log(proc.stdout.strip())
            break
        last_error = proc.stderr.strip() or proc.stdout.strip() or f"pip exited with code {proc.returncode}"

    if not updated_successfully:
        startup_log("WARNING: Package auto-update failed. Continuing with current installation.")
        startup_log("TIP: On macOS, this script is happiest inside a virtual environment.")
        if last_error:
            startup_log(last_error)
        startup_log()
        return

    write_last_update_ts(time.time())
    startup_log("> Package check/update completed. Restarting once so Python uses the latest installed yt-dlp...")
    new_env = os.environ.copy()
    new_env[RESTARTED_AFTER_UPDATE_ENV] = "1"
    script_path = str(Path(__file__).resolve())
    os.execvpe(sys.executable, [sys.executable, script_path, *sys.argv[1:]], new_env)


run_auto_update_before_import()

try:
    from yt_dlp import YoutubeDL  # type: ignore
    try:
        from yt_dlp.version import __version__ as YTDLP_VERSION  # type: ignore
    except Exception:
        YTDLP_VERSION = "unknown"
except ImportError:
    root = tk.Tk(className="SmartYTDownloaderMac")
    root.withdraw()
    messagebox.showerror(
        "yt-dlp not installed",
        "yt-dlp is not installed for the Python running this app.\n\n"
        "Recommended on macOS:\n"
        "  python3 -m venv ~/venvs/ytdlp-gui\n"
        "  source ~/venvs/ytdlp-gui/bin/activate\n"
        "  python -m pip install -U pip yt-dlp yt-dlp-ejs\n",
    )
    raise SystemExit(1)


def load_gui_state() -> dict:
    try:
        return json.loads(APP_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_gui_state(data: dict) -> None:
    ensure_state_dir()
    APP_STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


YOUTUBE_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")


def normalize_youtube_watch_url(url: str) -> str:
    url = (url or "").strip()

    direct_patterns = (
        r"(?:youtube\.com/watch\?[^\s#]*?v=)([A-Za-z0-9_-]{11})",
        r"(?:youtu\.be/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/shorts/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/embed/)([A-Za-z0-9_-]{11})",
    )
    for pattern in direct_patterns:
        match = re.search(pattern, url)
        if match:
            return f"https://www.youtube.com/watch?v={match.group(1)}"

    try:
        parsed = urlparse(url)
    except Exception:
        return url

    host = parsed.netloc.lower()
    if "youtube.com" in host and parsed.path == "/watch":
        qs = parse_qs(parsed.query)
        raw_video_id = qs.get("v", [None])[0]
        if raw_video_id:
            match = YOUTUBE_ID_RE.search(raw_video_id)
            if match:
                return f"https://www.youtube.com/watch?v={match.group(0)}"
    return url


def parse_positive_int(token: str, name_for_error: str, logger) -> int | None:
    token = token.strip()
    if not token:
        return None
    try:
        value = int(token)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        logger(f"Invalid {name_for_error!r} '{token}'. Ignoring.")
        return None


def format_bytes(num: float | int | None) -> str:
    if not num:
        return "?"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(num)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"


def format_seconds(seconds: float | int | None) -> str:
    if seconds is None:
        return "?"
    try:
        total = int(seconds)
    except Exception:
        return "?"
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:d}:{sec:02d}"


def check_ffmpeg(logger) -> None:
    if shutil.which("ffmpeg") is None and shutil.which("avconv") is None:
        logger(
            "WARNING: 'ffmpeg' not found on PATH. Install it so yt-dlp can merge/convert media, e.g.:\n"
            "  brew install ffmpeg"
        )
    else:
        logger("> ffmpeg detected.")


def check_js_runtime(logger) -> None:
    runtimes = ("deno", "node", "quickjs", "bun")
    found = [runtime for runtime in runtimes if shutil.which(runtime)]
    if found:
        logger(f"> Detected JavaScript runtime for yt-dlp: {found[0]}")
    else:
        logger(
            "WARNING: No JavaScript runtime (deno/node/quickjs/bun) detected.\n"
            "For more reliable YouTube downloads, install one, e.g. on macOS:\n"
            "  brew install node"
        )


def build_js_runtime_opts(logger) -> dict | None:
    deno_path = shutil.which("deno")
    node_path = shutil.which("node")
    quickjs_path = shutil.which("qjs") or shutil.which("quickjs")
    bun_path = shutil.which("bun")

    if deno_path:
        logger(f"> Enabling yt-dlp JS runtime: deno ({deno_path})")
        return {"deno": {"path": deno_path}}
    if node_path:
        logger(f"> Enabling yt-dlp JS runtime: node ({node_path})")
        return {"node": {"path": node_path}}
    if quickjs_path:
        logger(f"> Enabling yt-dlp JS runtime: quickjs ({quickjs_path})")
        return {"quickjs": {"path": quickjs_path}}
    if bun_path:
        logger(f"> Enabling yt-dlp JS runtime: bun ({bun_path})")
        return {"bun": {"path": bun_path}}

    logger("> No JS runtime could be enabled for yt-dlp. YouTube downloads may be limited or fail.")
    return None


def check_yt_dlp_version(logger) -> None:
    logger(f"> yt-dlp version detected by this app: {YTDLP_VERSION}")


def print_youtube_fix_hint(logger) -> None:
    logger(
        "NOTE: This looks like a YouTube/yt-dlp extraction issue, not just a bad URL.\n"
        "Try updating yt-dlp first. If you are using a virtual environment:\n"
        "  python -m pip install -U yt-dlp yt-dlp-ejs\n"
        "This build now avoids forcing the old audio-only <=192 kbps format selector and lets yt-dlp fetch YouTube webpage data.\n"
        "If one video still fails, try disabling browser cookies for public videos, or use a different logged-in browser profile."
    )


def looks_like_youtube_extraction_breakage(message: str) -> bool:
    lowered = message.lower()
    markers = (
        "requested format is not available",
        "only images are available",
        "no video formats",
        "no formats",
        "po token",
        "missing required visitor data",
        "n challenge",
        "signature solving failed",
        "sign in to confirm you're not a bot",
        "unable to extract initial data",
        "http error 403",
    )
    return any(marker in lowered for marker in markers)


def build_youtube_extractor_args(logger) -> dict:
    """Return conservative YouTube extractor args.

    Do not skip YouTube webpage/config requests here. Recent YouTube extractor
    behavior can need visitor data/PO-token-related context, and skipping the
    webpage can make yt-dlp see fewer usable formats.
    """
    logger(
        "> YouTube extractor: using yt-dlp defaults; not skipping webpage/config requests "
        "so visitor data can be discovered when available."
    )
    return {}


class GuiLogger:
    def __init__(self, sink) -> None:
        self.sink = sink

    def debug(self, msg: str) -> None:
        cleaned = (msg or "").strip()
        if cleaned:
            self.sink(cleaned)

    def info(self, msg: str) -> None:
        cleaned = (msg or "").strip()
        if cleaned:
            self.sink(cleaned)

    def warning(self, msg: str) -> None:
        cleaned = (msg or "").strip()
        if cleaned:
            self.sink(f"WARNING: {cleaned}")

    def error(self, msg: str) -> None:
        cleaned = (msg or "").strip()
        if cleaned:
            self.sink(f"ERROR: {cleaned}")


def build_subtitle_opts(*, langs: list[str], auto: bool, skip_download: bool = False) -> dict:
    opts: dict = {
        "subtitleslangs": langs,
        "subtitlesformat": "srt",
        "convertsubtitles": "srt",
        "writesubtitles": not auto,
        "writeautomaticsub": auto,
    }
    if skip_download:
        opts["skip_download"] = True
        opts["nooverwrites"] = True
    return opts


def maybe_get_playlist_length(url: str, extractor_args: dict, cookiesfrombrowser, logger) -> int | None:
    try:
        info_opts: dict = {
            "quiet": True,
            "ignoreerrors": True,
            "extract_flat": "in_playlist",
            "windowsfilenames": True,
        }
        if extractor_args:
            info_opts["extractor_args"] = extractor_args
        if cookiesfrombrowser is not None:
            info_opts["cookiesfrombrowser"] = cookiesfrombrowser

        with YoutubeDL(info_opts) as ydl_info:
            info = ydl_info.extract_info(url, download=False)
        if info and info.get("_type") == "playlist":
            entries = info.get("entries") or []
            return info.get("n_entries") or info.get("playlist_count") or sum(1 for entry in entries if entry)
    except Exception as exc:
        logger(f"NOTE: Could not pre-fetch playlist length; using 2-digit numbering. ({exc})")
    return None


def run_download(urls: list[str], ydl_opts: dict, *, retry_without_cookies: bool, logger) -> int:
    try:
        with YoutubeDL(ydl_opts) as ydl:
            result = ydl.download(urls)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        message = str(exc)
        if retry_without_cookies and ydl_opts.get("cookiesfrombrowser"):
            logger(
                "> Initial attempt failed while using browser cookies.\n"
                "> Retrying once without cookies (this often works for normal public videos)…"
            )
            retry_opts = {**ydl_opts}
            retry_opts.pop("cookiesfrombrowser", None)
            try:
                with YoutubeDL(retry_opts) as ydl:
                    retry_result = ydl.download(urls)
            except KeyboardInterrupt:
                raise
            except Exception as retry_exc:
                retry_message = str(retry_exc)
                if looks_like_youtube_extraction_breakage(message) or looks_like_youtube_extraction_breakage(retry_message):
                    print_youtube_fix_hint(logger)
                logger(f"ERROR: {retry_message}")
                return 1
            else:
                if retry_result == 0:
                    logger("> Retry without cookies succeeded.")
                    return 0
                if looks_like_youtube_extraction_breakage(message):
                    print_youtube_fix_hint(logger)
                logger("ERROR: yt-dlp reported a failure during the retry.")
                return retry_result or 1

        if looks_like_youtube_extraction_breakage(message):
            print_youtube_fix_hint(logger)
        logger(f"ERROR: {message}")
        return 1
    else:
        if result:
            if retry_without_cookies and ydl_opts.get("cookiesfrombrowser"):
                logger(
                    "> yt-dlp reported a failure while using browser cookies.\n"
                    "> Retrying once without cookies (this often works for normal public videos)…"
                )
                retry_opts = {**ydl_opts}
                retry_opts.pop("cookiesfrombrowser", None)
                with YoutubeDL(retry_opts) as ydl:
                    retry_result = ydl.download(urls)
                if retry_result == 0:
                    logger("> Retry without cookies succeeded.")
                    return 0
            if looks_like_youtube_extraction_breakage(str(result)):
                print_youtube_fix_hint(logger)
            logger("ERROR: yt-dlp reported one or more download failures.")
            return result
        return 0


TIME_RE = re.compile(r"(?P<s>\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(?P<e>\d{2}:\d{2}:\d{2},\d{3})")


@dataclass
class SrtCue:
    start_ms: int
    end_ms: int
    text_lines: list[str]


@dataclass
class SubtitleCleanupStats:
    scanned_files: int = 0
    adjusted_files: int = 0
    changed_cues: int = 0
    failed_files: int = 0


@dataclass
class ResidueCleanupStats:
    scanned_files: int = 0
    removed_files: int = 0
    failed_files: int = 0


def snapshot_all_files(directory: str | Path) -> set[Path]:
    base = Path(directory).expanduser()
    if not base.exists():
        return set()
    return {path.resolve() for path in base.rglob("*") if path.is_file()}


def looks_like_yt_dlp_residue(path: Path) -> bool:
    name = path.name.lower()

    if name.endswith('.part') or name.endswith('.ytdl'):
        return True
    if '.temp.' in name or name.endswith('.temp'):
        return True
    if re.search(r'\.f\d{2,6}\.', name):
        return True
    if re.search(r'\.frag\d{1,6}\.', name):
        return True
    if name.endswith('.aria2'):
        return True

    return False


def cleanup_yt_dlp_residue_files(files: list[Path], logger) -> ResidueCleanupStats:
    stats = ResidueCleanupStats(scanned_files=len(files))
    if not files:
        logger('> Residue cleanup: no candidate files were found.')
        return stats

    for path in sorted(files):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except Exception as exc:
            stats.failed_files += 1
            logger(f"WARNING: Could not remove residue '{path.name}': {exc}")
        else:
            stats.removed_files += 1
            logger(f"> Removed residue: {path.name}")

    if stats.removed_files:
        logger(f"> Residue cleanup removed {stats.removed_files} file(s).")
    else:
        logger('> Residue cleanup removed 0 files.')
    if stats.failed_files:
        logger(f"> Residue cleanup could not remove {stats.failed_files} file(s).")
    return stats


def cleanup_new_residue_since(before_files: set[Path], directory: str | Path, logger) -> ResidueCleanupStats:
    after_files = snapshot_all_files(directory)
    new_files = sorted(after_files - before_files)
    residue_files = [path for path in new_files if looks_like_yt_dlp_residue(path)]
    if residue_files:
        logger('> Auto residue cleanup: removing new yt-dlp temporary/intermediate files left behind...')
    return cleanup_yt_dlp_residue_files(residue_files, logger)


def find_residue_files_in_directory(directory: str | Path) -> list[Path]:
    base = Path(directory).expanduser()
    if not base.exists():
        return []
    return sorted(path.resolve() for path in base.rglob('*') if path.is_file() and looks_like_yt_dlp_residue(path))


def parse_srt_time(value: str) -> int:
    hh, mm, rest = value.split(":")
    ss, ms = rest.split(",")
    return (int(hh) * 3600 + int(mm) * 60 + int(ss)) * 1000 + int(ms)


def format_srt_time(ms: int) -> str:
    if ms < 0:
        ms = 0
    hh = ms // 3_600_000
    ms %= 3_600_000
    mm = ms // 60_000
    ms %= 60_000
    ss = ms // 1000
    mmm = ms % 1000
    return f"{hh:02d}:{mm:02d}:{ss:02d},{mmm:03d}"


def parse_srt_content(content: str) -> list[SrtCue]:
    content = content.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    blocks = re.split(r"\n\s*\n", content.strip(), flags=re.M)

    cues: list[SrtCue] = []
    for block in blocks:
        lines = block.splitlines()
        if not lines:
            continue

        time_line_idx = None
        match = None
        for i, line in enumerate(lines):
            match = TIME_RE.search(line.strip())
            if match:
                time_line_idx = i
                break

        if time_line_idx is None or match is None:
            continue

        cues.append(
            SrtCue(
                start_ms=parse_srt_time(match.group("s")),
                end_ms=parse_srt_time(match.group("e")),
                text_lines=lines[time_line_idx + 1:],
            )
        )
    return cues


def write_srt_content(cues: list[SrtCue]) -> str:
    out: list[str] = []
    for i, cue in enumerate(cues, start=1):
        out.append(str(i))
        out.append(f"{format_srt_time(cue.start_ms)} --> {format_srt_time(cue.end_ms)}")
        out.extend(cue.text_lines if cue.text_lines else [""])
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def trim_ends_to_remove_overlaps(
    cues: list[SrtCue], *, gap_ms: int = 1, min_cue_ms: int = 1
) -> tuple[list[SrtCue], int, int]:
    changed = 0
    if not cues:
        return cues, changed, 0

    for i in range(len(cues) - 1):
        next_start = cues[i + 1].start_ms
        desired_end = next_start - gap_ms

        if cues[i].end_ms > desired_end:
            new_end = max(cues[i].start_ms + min_cue_ms, desired_end)
            if new_end != cues[i].end_ms:
                cues[i].end_ms = new_end
                changed += 1

        if cues[i].end_ms < cues[i].start_ms + min_cue_ms:
            cues[i].end_ms = cues[i].start_ms + min_cue_ms
            changed += 1

    if cues[-1].end_ms < cues[-1].start_ms + min_cue_ms:
        cues[-1].end_ms = cues[-1].start_ms + min_cue_ms
        changed += 1

    remaining = sum(1 for i in range(len(cues) - 1) if cues[i].end_ms > cues[i + 1].start_ms)
    return cues, changed, remaining


def atomic_write_text(path: Path, data: str, *, encoding: str = "utf-8") -> None:
    path = path.resolve()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as handle:
            handle.write(data)
        os.replace(str(tmp_path), str(path))
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def fix_srt_overlaps_file(path: Path, *, gap_ms: int = 1, min_cue_ms: int = 1) -> dict:
    content = path.read_text(encoding="utf-8", errors="replace")
    cues = parse_srt_content(content)
    if not cues:
        raise ValueError("No cues parsed. Is this a valid .srt?")

    original_overlaps = sum(1 for i in range(len(cues) - 1) if cues[i].end_ms > cues[i + 1].start_ms)
    cues, changed, remaining = trim_ends_to_remove_overlaps(cues, gap_ms=gap_ms, min_cue_ms=min_cue_ms)

    if changed:
        atomic_write_text(path, write_srt_content(cues), encoding="utf-8")

    return {
        "cue_count": len(cues),
        "original_overlaps": original_overlaps,
        "changed_cues": changed,
        "remaining_overlaps": remaining,
    }


def snapshot_srt_files(directory: str | Path) -> set[Path]:
    base = Path(directory).expanduser()
    if not base.exists():
        return set()
    return {path.resolve() for path in base.rglob("*.srt") if path.is_file()}


def run_auto_subtitle_cleanup(new_srt_files: list[Path], logger) -> SubtitleCleanupStats:
    stats = SubtitleCleanupStats(scanned_files=len(new_srt_files))
    if not new_srt_files:
        logger("> No new AUTO-generated .srt files were created during fallback, so no subtitle cleanup was needed.")
        return stats

    logger(
        f"> AUTO-generated subtitle cleanup: found {len(new_srt_files)} new .srt file(s). "
        "Fixing overlap flow automatically..."
    )

    for path in new_srt_files:
        try:
            result = fix_srt_overlaps_file(path)
        except Exception as exc:
            stats.failed_files += 1
            logger(f"WARNING: Could not clean subtitle flow for '{path.name}': {exc}")
            continue

        changed_cues = int(result["changed_cues"])
        stats.changed_cues += changed_cues
        if changed_cues:
            stats.adjusted_files += 1
            logger(
                f"> Cleaned AUTO subtitles: {path.name} "
                f"({changed_cues} cue end-time adjustment(s), {result['original_overlaps']} overlap(s) removed)."
            )
        else:
            logger(f"> AUTO subtitles already looked fine: {path.name}")

    if stats.failed_files:
        logger(f"> Subtitle cleanup finished with {stats.failed_files} file(s) that could not be processed.")
    else:
        logger("> Subtitle cleanup finished.")

    return stats


def run_optional_auto_sub_fallback(
    urls: list[str],
    base_opts: dict,
    *,
    output_dir: str | Path,
    retry_without_cookies: bool,
    logger,
) -> SubtitleCleanupStats:
    fallback_opts = {
        **base_opts,
        **build_subtitle_opts(langs=base_opts["subtitleslangs"], auto=True, skip_download=True),
        "ignoreerrors": True,
    }
    before_files = snapshot_srt_files(output_dir)
    logger("> Subtitle fallback pass: trying AUTO-generated subtitles only where manual subtitles were not created...")
    result = run_download(urls, fallback_opts, retry_without_cookies=retry_without_cookies, logger=logger)
    after_files = snapshot_srt_files(output_dir)
    new_auto_srt_files = sorted(after_files - before_files)
    cleanup_stats = run_auto_subtitle_cleanup(new_auto_srt_files, logger)
    if result != 0:
        logger(
            "> Auto-subtitle fallback did not fully succeed. That is not fatal: your media download may still be fine, "
            "and manual subtitles may already be present for the items that have them."
        )
    return cleanup_stats


PALETTE = {
    "window_bg": "#F3F4F6",
    "header_bg": "#E5E7EB",
    "card_bg": "#FFFFFF",
    "field_bg": "#FFFFFF",
    "text": "#111827",
    "muted_text": "#374151",
    "border": "#4B5563",
    "button_bg": "#E5E7EB",
    "button_hover": "#D1D5DB",
    "button_pressed": "#9CA3AF",
    "button_text": "#111827",
    "close_bg": "#D1D5DB",
    "close_hover": "#9CA3AF",
    "close_pressed": "#6B7280",
    "close_text": "#111827",
    "disabled_bg": "#D1D5DB",
    "disabled_text": "#374151",
    "accent": "#FDE047",
    "accent_hover": "#FACC15",
    "accent_pressed": "#EAB308",
    "accent_text": "#111827",
    "danger": "#B42318",
    "danger_hover": "#912018",
    "danger_pressed": "#7A1A13",
    "danger_text": "#FFFFFF",
    "focus": "#111827",
}


def _pick_first_available_font(root: tk.Misc, candidates: tuple[str, ...], fallback: str) -> str:
    try:
        families = {family.lower(): family for family in tkfont.families(root)}
    except tk.TclError:
        return fallback
    for candidate in candidates:
        match = families.get(candidate.lower())
        if match:
            return match
    return fallback


class AccessibleButton(tk.Button):
    def __init__(self, master, text: str, command, *, kind: str = "neutral", font=None, **kwargs):
        self.kind = kind

        if kind == "accent":
            bg = PALETTE["accent"]
            fg = PALETTE["accent_text"]
            active_bg = PALETTE["accent_hover"]
            active_fg = PALETTE["accent_text"]
        elif kind == "danger":
            bg = PALETTE["danger"]
            fg = PALETTE["danger_text"]
            active_bg = PALETTE["danger_hover"]
            active_fg = PALETTE["danger_text"]
        elif kind == "close":
            bg = PALETTE["close_bg"]
            fg = PALETTE["close_text"]
            active_bg = PALETTE["close_hover"]
            active_fg = PALETTE["close_text"]
        else:
            bg = PALETTE["button_bg"]
            fg = PALETTE["button_text"]
            active_bg = PALETTE["button_hover"]
            active_fg = PALETTE["button_text"]

        super().__init__(
            master,
            text=text,
            command=command,
            font=font,
            bg=bg,
            fg=fg,
            activebackground=active_bg,
            activeforeground=active_fg,
            disabledforeground=PALETTE["disabled_text"],
            relief="solid",
            bd=2,
            highlightthickness=3,
            highlightbackground=PALETTE["border"],
            highlightcolor=PALETTE["focus"],
            padx=16,
            pady=10,
            cursor="hand2",
            takefocus=True,
            **kwargs,
        )

        self._normal_bg = bg
        self._normal_fg = fg
        self._active_bg = active_bg
        self._active_fg = active_fg

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _on_enter(self, _event):
        if str(self["state"]) != "disabled":
            self.configure(bg=self._active_bg, fg=self._active_fg)

    def _on_leave(self, _event):
        if str(self["state"]) != "disabled":
            self.configure(bg=self._normal_bg, fg=self._normal_fg)

    def _on_press(self, _event):
        if str(self["state"]) != "disabled":
            if self.kind == "accent":
                self.configure(bg=PALETTE["accent_pressed"], fg=PALETTE["accent_text"])
            elif self.kind == "danger":
                self.configure(bg=PALETTE["danger_pressed"], fg=PALETTE["danger_text"])
            elif self.kind == "close":
                self.configure(bg=PALETTE["close_pressed"], fg=PALETTE["close_text"])
            else:
                self.configure(bg=PALETTE["button_pressed"], fg=PALETTE["button_text"])

    def _on_release(self, _event):
        if str(self["state"]) != "disabled":
            self.configure(bg=self._active_bg, fg=self._active_fg)

    def set_busy(self, busy: bool, text: str | None = None):
        if text is not None:
            self.configure(text=text)

        if self.kind == "accent":
            normal_bg = PALETTE["accent"]
            normal_fg = PALETTE["accent_text"]
            hover_bg = PALETTE["accent_hover"]
            hover_fg = PALETTE["accent_text"]
        elif self.kind == "danger":
            normal_bg = PALETTE["danger"]
            normal_fg = PALETTE["danger_text"]
            hover_bg = PALETTE["danger_hover"]
            hover_fg = PALETTE["danger_text"]
        elif self.kind == "close":
            normal_bg = PALETTE["close_bg"]
            normal_fg = PALETTE["close_text"]
            hover_bg = PALETTE["close_hover"]
            hover_fg = PALETTE["close_text"]
        else:
            normal_bg = PALETTE["button_bg"]
            normal_fg = PALETTE["button_text"]
            hover_bg = PALETTE["button_hover"]
            hover_fg = PALETTE["button_text"]

        if busy:
            self.configure(
                state="normal",
                bg=normal_bg,
                fg=normal_fg,
                activebackground=normal_bg,
                activeforeground=normal_fg,
                cursor="arrow",
            )
        else:
            self.configure(
                state="normal",
                bg=normal_bg,
                fg=normal_fg,
                activebackground=hover_bg,
                activeforeground=hover_fg,
                cursor="hand2",
            )

        self._normal_bg = normal_bg
        self._normal_fg = normal_fg
        self._active_bg = hover_bg
        self._active_fg = hover_fg


def apply_theme(root: tk.Tk) -> dict:
    palette = dict(PALETTE)
    style = ttk.Style(root)

    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    sans_font = _pick_first_available_font(
        root,
        ("SF Pro Text", "Helvetica Neue", "Helvetica", "Arial", "Inter"),
        "TkDefaultFont",
    )
    mono_font = _pick_first_available_font(
        root,
        ("SF Mono", "Menlo", "Monaco", "Courier New"),
        "TkFixedFont",
    )

    fonts = {
        "base": (sans_font, 14),
        "small": (sans_font, 13),
        "title": (sans_font, 22, "bold"),
        "subtitle": (sans_font, 13),
        "mono_base": (mono_font, 13),
        "button": (sans_font, 14, "bold"),
    }

    root.configure(bg=palette["window_bg"])
    root.option_add("*tearOff", False)

    default_font = tkfont.nametofont("TkDefaultFont")
    default_font.configure(family=sans_font, size=14)

    style.configure(".", background=palette["window_bg"], foreground=palette["text"], font=fonts["base"])
    style.configure("App.TFrame", background=palette["window_bg"])
    style.configure("Header.TFrame", background=palette["header_bg"])
    style.configure("HeaderTitle.TLabel", background=palette["header_bg"], foreground=palette["text"], font=fonts["title"])
    style.configure("HeaderSubtitle.TLabel", background=palette["header_bg"], foreground=palette["muted_text"], font=fonts["subtitle"])
    style.configure("Card.TLabelframe", background=palette["card_bg"], relief="solid", borderwidth=1)
    style.configure("Card.TLabelframe.Label", background=palette["card_bg"], foreground=palette["text"], font=(sans_font, 13, "bold"))
    style.configure("Card.TFrame", background=palette["card_bg"])
    style.configure("Card.TLabel", background=palette["card_bg"], foreground=palette["text"], font=fonts["base"])
    style.configure("Muted.Card.TLabel", background=palette["card_bg"], foreground=palette["muted_text"], font=fonts["small"])
    style.configure("Card.TRadiobutton", background=palette["card_bg"], foreground=palette["text"], font=fonts["base"])
    style.map("Card.TRadiobutton", background=[("active", palette["card_bg"])], foreground=[("disabled", palette["disabled_text"])])
    style.configure("Card.TCheckbutton", background=palette["card_bg"], foreground=palette["text"], font=fonts["base"])
    style.map("Card.TCheckbutton", background=[("active", palette["card_bg"])], foreground=[("disabled", palette["disabled_text"])])

    style.configure(
        "TEntry",
        fieldbackground=palette["field_bg"],
        background=palette["field_bg"],
        foreground=palette["text"],
        insertcolor=palette["text"],
        borderwidth=1,
        padding=10,
        relief="solid",
    )
    style.configure(
        "TCombobox",
        fieldbackground=palette["field_bg"],
        background=palette["field_bg"],
        foreground=palette["text"],
        arrowcolor=palette["text"],
        padding=10,
        relief="solid",
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", palette["field_bg"]), ("disabled", palette["disabled_bg"])],
        foreground=[("disabled", palette["disabled_text"])],
    )

    style.configure("Status.TLabel", background=palette["card_bg"], foreground=palette["text"], font=(sans_font, 14, "bold"))
    style.configure("Detail.TLabel", background=palette["card_bg"], foreground=palette["muted_text"], font=fonts["small"])
    style.configure(
        "Horizontal.TProgressbar",
        background="#0B5FFF",
        troughcolor="#D1D5DB",
        borderwidth=0,
        lightcolor="#0B5FFF",
        darkcolor="#0B5FFF",
    )

    return {"palette": palette, "fonts": fonts, "style": style}


class DownloaderGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Smart YouTube Downloader (macOS)")
        self.root.geometry("1220x980")
        self.root.minsize(1040, 820)

        self.theme = apply_theme(self.root)
        self.palette = self.theme["palette"]
        self.fonts = self.theme["fonts"]

        self.queue: queue.Queue[tuple] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.downloading = False

        state = load_gui_state()

        self.mode_var = tk.StringVar(value=state.get("mode", "playlist"))
        self.media_var = tk.StringVar(value=state.get("media_type", "video"))
        self.url_var = tk.StringVar(value=state.get("url", ""))
        self.output_dir_var = tk.StringVar(value=state.get("output_dir", str(Path.home() / "Downloads")))
        self.use_cookies_var = tk.BooleanVar(value=state.get("use_cookies", True))
        self.browser_var = tk.StringVar(value=state.get("browser", "firefox"))
        self.want_subs_var = tk.BooleanVar(value=state.get("want_subs", False))
        self.subs_lang_var = tk.StringVar(value=state.get("subs_lang", "en"))
        self.start_idx_var = tk.StringVar(value=state.get("playlist_start", ""))
        self.end_idx_var = tk.StringVar(value=state.get("playlist_end", ""))
        self.status_var = tk.StringVar(value="Ready.")
        self.progress_text_var = tk.StringVar(value="Idle")

        self.build_ui()
        self.apply_state_rules()

        for msg in STARTUP_MESSAGES:
            if msg:
                self.append_log(msg)

        self.root.after(100, self.process_queue)

    def build_ui(self) -> None:
        main = ttk.Frame(self.root, style="App.TFrame")
        main.pack(fill="both", expand=True)

        canvas_holder = ttk.Frame(main, style="App.TFrame")
        canvas_holder.pack(fill="both", expand=True)

        self.app_canvas = tk.Canvas(
            canvas_holder,
            bg=self.palette["window_bg"],
            highlightthickness=0,
            bd=0,
            relief="flat",
        )
        self.app_canvas.pack(side="left", fill="both", expand=True)

        self.app_scrollbar = ttk.Scrollbar(canvas_holder, orient="vertical", command=self.app_canvas.yview)
        self.app_scrollbar.pack(side="right", fill="y")
        self.app_canvas.configure(yscrollcommand=self.app_scrollbar.set)

        self.scrollable_frame = ttk.Frame(self.app_canvas, padding=14, style="App.TFrame")
        self._scrollable_window = self.app_canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")

        self.scrollable_frame.bind("<Configure>", self.on_scrollable_frame_configure)
        self.app_canvas.bind("<Configure>", self.on_app_canvas_configure)

        self.root.bind_all("<MouseWheel>", self.on_global_mousewheel, add="+")
        self.root.bind_all("<Shift-MouseWheel>", self.on_global_shift_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self.on_global_mousewheel_linux, add="+")
        self.root.bind_all("<Button-5>", self.on_global_mousewheel_linux, add="+")

        header = ttk.Frame(self.scrollable_frame, padding=(18, 14), style="Header.TFrame")
        header.pack(fill="x")

        header_left = ttk.Frame(header, style="Header.TFrame")
        header_left.pack(side="left", fill="x", expand=True)
        ttk.Label(header_left, text="Smart YouTube Downloader", style="HeaderTitle.TLabel").pack(anchor="w")
        ttk.Label(
            header_left,
            text="High-contrast macOS version. The download button uses black text on yellow.",
            style="HeaderSubtitle.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        body = ttk.Frame(self.scrollable_frame, padding=(0, 12, 0, 0), style="App.TFrame")
        body.pack(fill="both", expand=True)

        settings = ttk.LabelFrame(body, text="Download settings", padding=14, style="Card.TLabelframe")
        settings.pack(fill="x")
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(3, weight=1)

        ttk.Label(settings, text="Mode:", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=6)
        mode_frame = ttk.Frame(settings, style="Card.TFrame")
        mode_frame.grid(row=0, column=1, sticky="w", pady=6)
        ttk.Radiobutton(mode_frame, text="Playlist", value="playlist", variable=self.mode_var, command=self.apply_state_rules, style="Card.TRadiobutton").pack(side="left")
        ttk.Radiobutton(mode_frame, text="Single video", value="single", variable=self.mode_var, command=self.apply_state_rules, style="Card.TRadiobutton").pack(side="left", padx=(14, 0))

        ttk.Label(settings, text="Media:", style="Card.TLabel").grid(row=0, column=2, sticky="w", padx=(16, 8), pady=6)
        media_frame = ttk.Frame(settings, style="Card.TFrame")
        media_frame.grid(row=0, column=3, sticky="w", pady=6)
        ttk.Radiobutton(media_frame, text="Video (<=1080p)", value="video", variable=self.media_var, command=self.apply_state_rules, style="Card.TRadiobutton").pack(side="left")
        ttk.Radiobutton(media_frame, text="Audio-only (MP3 ~192 kbps)", value="audio", variable=self.media_var, command=self.apply_state_rules, style="Card.TRadiobutton").pack(side="left", padx=(14, 0))
        ttk.Radiobutton(media_frame, text="SRT-only (subtitles only)", value="srt", variable=self.media_var, command=self.apply_state_rules, style="Card.TRadiobutton").pack(side="left", padx=(14, 0))

        ttk.Label(settings, text="YouTube URL:", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=6)
        ttk.Entry(settings, textvariable=self.url_var).grid(row=1, column=1, columnspan=3, sticky="ew", pady=6)

        ttk.Label(settings, text="Output folder:", style="Card.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=6)
        ttk.Entry(settings, textvariable=self.output_dir_var).grid(row=2, column=1, columnspan=2, sticky="ew", pady=6)

        browse_btn = AccessibleButton(settings, text="Browse…", command=self.choose_output_dir, kind="neutral", font=self.fonts["button"])
        browse_btn.grid(row=2, column=3, sticky="e", pady=6)

        self.cookies_check = ttk.Checkbutton(settings, text="Use browser cookies", variable=self.use_cookies_var, command=self.apply_state_rules, style="Card.TCheckbutton")
        self.cookies_check.grid(row=3, column=0, sticky="w", pady=6)

        ttk.Label(settings, text="Browser:", style="Card.TLabel").grid(row=3, column=1, sticky="e", padx=(0, 8), pady=6)
        self.browser_combo = ttk.Combobox(settings, textvariable=self.browser_var, values=SUPPORTED_BROWSERS, state="readonly", width=14)
        self.browser_combo.grid(row=3, column=2, sticky="w", pady=6)

        self.subs_check = ttk.Checkbutton(settings, text="Download subtitles (.srt)", variable=self.want_subs_var, command=self.apply_state_rules, style="Card.TCheckbutton")
        self.subs_check.grid(row=4, column=0, sticky="w", pady=6)

        ttk.Label(settings, text="Subtitle language:", style="Card.TLabel").grid(row=4, column=1, sticky="e", padx=(0, 8), pady=6)
        self.subs_lang_entry = ttk.Entry(settings, textvariable=self.subs_lang_var, width=10)
        self.subs_lang_entry.grid(row=4, column=2, sticky="w", pady=6)

        playlist_frame = ttk.LabelFrame(body, text="Playlist range (optional)", padding=14, style="Card.TLabelframe")
        playlist_frame.pack(fill="x", pady=(12, 0))

        ttk.Label(playlist_frame, text="From video #:", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.start_entry = ttk.Entry(playlist_frame, textvariable=self.start_idx_var, width=12)
        self.start_entry.grid(row=0, column=1, sticky="w", pady=4)

        ttk.Label(playlist_frame, text="To video #:", style="Card.TLabel").grid(row=0, column=2, sticky="w", padx=(16, 8), pady=4)
        self.end_entry = ttk.Entry(playlist_frame, textvariable=self.end_idx_var, width=12)
        self.end_entry.grid(row=0, column=3, sticky="w", pady=4)

        ttk.Label(playlist_frame, text="Leave both blank to download the full playlist.", style="Muted.Card.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        actions = tk.Frame(body, bg=self.palette["window_bg"])
        actions.pack(fill="x", pady=(12, 0))

        self.start_button = AccessibleButton(
            actions,
            text="Start download",
            command=self.start_download,
            kind="accent",
            font=self.fonts["button"],
        )
        self.start_button.pack(side="left")
        self.start_button.configure(
            bg="#FDE047",
            fg="#111827",
            activebackground="#FACC15",
            activeforeground="#111827",
            highlightbackground="#111827",
            highlightcolor="#111827",
        )

        AccessibleButton(actions, text="Open output folder", command=self.open_output_folder, kind="neutral", font=self.fonts["button"]).pack(side="left", padx=(8, 0))
        AccessibleButton(actions, text="Clean residue", command=self.clean_residue_files, kind="neutral", font=self.fonts["button"]).pack(side="left", padx=(8, 0))
        AccessibleButton(actions, text="Clear log", command=self.clear_log, kind="neutral", font=self.fonts["button"]).pack(side="left", padx=(8, 0))
        close_button = AccessibleButton(
            actions,
            text="Close window",
            command=self.root.destroy,
            kind="close",
            font=self.fonts["button"],
        )
        close_button.pack(side="right")
        close_button.configure(
            bg="#D1D5DB",
            fg="#111827",
            activebackground="#9CA3AF",
            activeforeground="#111827",
            highlightbackground="#111827",
            highlightcolor="#111827",
        )

        progress_frame = ttk.LabelFrame(body, text="Progress", padding=14, style="Card.TLabelframe")
        progress_frame.pack(fill="x", pady=(12, 0))
        ttk.Label(progress_frame, textvariable=self.status_var, style="Status.TLabel").pack(anchor="w")
        self.progress = ttk.Progressbar(progress_frame, orient="horizontal", mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(8, 4))
        ttk.Label(progress_frame, textvariable=self.progress_text_var, style="Detail.TLabel").pack(anchor="w")

        log_frame = ttk.LabelFrame(body, text="Log", padding=14, style="Card.TLabelframe")
        log_frame.pack(fill="both", expand=True, pady=(12, 0))
        ttk.Label(
            log_frame,
            text="The whole window now scrolls, and the log keeps its own vertical + horizontal scrolling.",
            style="Muted.Card.TLabel",
        ).pack(anchor="w", pady=(0, 8))

        log_container = tk.Frame(log_frame, bg=self.palette["card_bg"])
        log_container.pack(fill="both", expand=True)
        log_container.grid_rowconfigure(0, weight=1)
        log_container.grid_columnconfigure(0, weight=1)

        self.log_text = tk.Text(
            log_container,
            wrap="none",
            height=30,
            font=self.fonts["mono_base"],
            bg=self.palette["card_bg"],
            fg=self.palette["text"],
            insertbackground=self.palette["text"],
            selectbackground="#BFDBFE",
            selectforeground=self.palette["text"],
            relief="solid",
            borderwidth=1,
            highlightthickness=2,
            highlightbackground=self.palette["border"],
            highlightcolor=self.palette["focus"],
            padx=10,
            pady=10,
            undo=False,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

        self.log_v_scrollbar = ttk.Scrollbar(log_container, orient="vertical", command=self.log_text.yview)
        self.log_v_scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_h_scrollbar = ttk.Scrollbar(log_container, orient="horizontal", command=self.log_text.xview)
        self.log_h_scrollbar.grid(row=1, column=0, sticky="ew")
        self.log_text.configure(yscrollcommand=self.log_v_scrollbar.set, xscrollcommand=self.log_h_scrollbar.set)
        self.log_text.configure(state="disabled")

    def on_scrollable_frame_configure(self, _event=None) -> None:
        self.app_canvas.configure(scrollregion=self.app_canvas.bbox("all"))

    def on_app_canvas_configure(self, event) -> None:
        self.app_canvas.itemconfigure(self._scrollable_window, width=event.width)
        self.on_scrollable_frame_configure()

    def widget_handles_its_own_scroll(self, widget) -> bool:
        scrollable_classes = {"Text", "Entry", "TEntry", "Combobox", "TCombobox", "Listbox", "Scrollbar"}
        current = widget
        while current is not None:
            try:
                if current == self.log_text:
                    return True
                if current.winfo_class() in scrollable_classes:
                    return True
                parent_name = current.winfo_parent()
                current = current.nametowidget(parent_name) if parent_name else None
            except Exception:
                return False
        return False

    def on_global_mousewheel(self, event) -> None:
        if self.widget_handles_its_own_scroll(event.widget):
            return
        delta = getattr(event, "delta", 0)
        if delta == 0:
            return
        step = -1 if delta > 0 else 1
        self.app_canvas.yview_scroll(step, "units")

    def on_global_shift_mousewheel(self, event) -> None:
        if self.widget_handles_its_own_scroll(event.widget):
            return
        delta = getattr(event, "delta", 0)
        if delta == 0:
            return
        step = -1 if delta > 0 else 1
        self.app_canvas.xview_scroll(step, "units")

    def on_global_mousewheel_linux(self, event) -> None:
        if self.widget_handles_its_own_scroll(event.widget):
            return
        num = getattr(event, "num", None)
        if num == 4:
            self.app_canvas.yview_scroll(-1, "units")
        elif num == 5:
            self.app_canvas.yview_scroll(1, "units")

    def apply_state_rules(self) -> None:
        playlist_enabled = self.mode_var.get() == "playlist"
        srt_only = self.media_var.get() == "srt"
        if srt_only and not self.want_subs_var.get():
            self.want_subs_var.set(True)
        subs_enabled = self.want_subs_var.get() or srt_only
        cookies_enabled = self.use_cookies_var.get()

        self.start_entry.configure(state="normal" if playlist_enabled else "disabled")
        self.end_entry.configure(state="normal" if playlist_enabled else "disabled")
        self.subs_lang_entry.configure(state="normal" if subs_enabled else "disabled")
        self.browser_combo.configure(state="readonly" if cookies_enabled else "disabled")

        if srt_only:
            self.subs_check.configure(state="disabled")
        else:
            self.subs_check.configure(state="normal")

    def choose_output_dir(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.output_dir_var.get() or str(Path.home()))
        if folder:
            self.output_dir_var.set(folder)

    def open_output_folder(self) -> None:
        folder = Path(self.output_dir_var.get()).expanduser()
        if not folder.exists():
            messagebox.showwarning("Folder not found", f"This folder does not exist:\n{folder}")
            return
        try:
            subprocess.Popen(["open", str(folder)])
        except Exception as exc:
            messagebox.showerror("Open folder failed", str(exc))

    def clean_residue_files(self) -> None:
        folder = Path(self.output_dir_var.get()).expanduser()
        if not folder.exists():
            messagebox.showwarning("Folder not found", f"This folder does not exist:\n{folder}")
            return

        residue_files = find_residue_files_in_directory(folder)
        if not residue_files:
            self.append_log("> Manual residue cleanup: nothing to remove in the selected output folder.")
            messagebox.showinfo("Clean residue", "No known yt-dlp residue files were found in this output folder.")
            return

        self.append_log("> Manual residue cleanup: removing known yt-dlp temporary/intermediate files from the output folder...")
        stats = cleanup_yt_dlp_residue_files(residue_files, self.append_log)
        messagebox.showinfo(
            "Clean residue",
            f"Removed {stats.removed_files} residue file(s)."
            + (f" Could not remove {stats.failed_files}." if stats.failed_files else ""),
        )

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def queue_log(self, message: str) -> None:
        self.queue.put(("log", message))

    def queue_progress(self, percent: float | None, text: str, indeterminate: bool = False) -> None:
        self.queue.put(("progress", percent, text, indeterminate))

    def process_queue(self) -> None:
        try:
            while True:
                item = self.queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.append_log(item[1])
                elif kind == "progress":
                    _, percent, text, indeterminate = item
                    self.progress_text_var.set(text)
                    if indeterminate:
                        if self.progress["mode"] != "indeterminate":
                            self.progress.configure(mode="indeterminate")
                            self.progress.start(10)
                    else:
                        if self.progress["mode"] != "determinate":
                            self.progress.stop()
                            self.progress.configure(mode="determinate")
                        self.progress["value"] = 0 if percent is None else max(0, min(100, float(percent)))
                elif kind == "status":
                    self.status_var.set(item[1])
                elif kind == "start":
                    self.downloading = True
                    self.start_button.set_busy(True, "Download in progress…")
                    self.start_button.configure(
                        bg="#FDE047",
                        fg="#111827",
                        activebackground="#FDE047",
                        activeforeground="#111827",
                    )
                    self.status_var.set("Download in progress...")
                elif kind == "done":
                    success, summary = item[1], item[2]
                    self.downloading = False
                    self.start_button.set_busy(False, "Start download")
                    self.start_button.configure(
                        bg="#FDE047",
                        fg="#111827",
                        activebackground="#FACC15",
                        activeforeground="#111827",
                    )
                    self.progress.stop()
                    if self.progress["mode"] != "determinate":
                        self.progress.configure(mode="determinate")
                    self.status_var.set(summary)
                    if success:
                        self.progress["value"] = 100
                    self.progress_text_var.set(summary)
                elif kind == "errorbox":
                    messagebox.showerror(item[1], item[2])
                elif kind == "infobox":
                    messagebox.showinfo(item[1], item[2])
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self.process_queue)

    def save_current_state(self) -> None:
        save_gui_state(
            {
                "mode": self.mode_var.get(),
                "media_type": self.media_var.get(),
                "url": self.url_var.get().strip(),
                "output_dir": self.output_dir_var.get().strip(),
                "use_cookies": self.use_cookies_var.get(),
                "browser": self.browser_var.get().strip() or "firefox",
                "want_subs": self.want_subs_var.get() or self.media_var.get() == "srt",
                "subs_lang": self.subs_lang_var.get().strip() or "en",
                "playlist_start": self.start_idx_var.get().strip(),
                "playlist_end": self.end_idx_var.get().strip(),
            }
        )

    def start_download(self) -> None:
        if self.downloading:
            return

        url = self.url_var.get().strip()
        output_dir = Path(self.output_dir_var.get().strip()).expanduser()

        if not url:
            messagebox.showwarning("Missing URL", "Please paste a YouTube video or playlist URL.")
            return
        if not output_dir:
            messagebox.showwarning("Missing output folder", "Please choose an output folder.")
            return

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror("Invalid output folder", str(exc))
            return

        if self.use_cookies_var.get() and self.browser_var.get().strip() not in SUPPORTED_BROWSERS:
            messagebox.showwarning("Invalid browser", "Choose one of: firefox, chrome, chromium, brave, edge, safari.")
            return

        want_subs = self.want_subs_var.get() or self.media_var.get() == "srt"
        if want_subs and not self.subs_lang_var.get().strip():
            messagebox.showwarning("Missing subtitle language", "Enter a subtitle language code such as en, fr, or ar.")
            return

        self.save_current_state()
        self.clear_log()
        self.queue.put(("start",))
        self.queue_progress(None, "Preparing download...", indeterminate=True)

        config = {
            "mode": self.mode_var.get(),
            "media_type": self.media_var.get(),
            "url": url,
            "output_dir": str(output_dir),
            "use_cookies": self.use_cookies_var.get(),
            "browser": self.browser_var.get().strip() or "firefox",
            "want_subs": self.want_subs_var.get() or self.media_var.get() == "srt",
            "subs_lang": self.subs_lang_var.get().strip() or "en",
            "start_idx": self.start_idx_var.get().strip(),
            "end_idx": self.end_idx_var.get().strip(),
        }

        self.worker = threading.Thread(target=self.download_worker, args=(config,), daemon=True)
        self.worker.start()

    def make_progress_hook(self):
        def hook(status: dict) -> None:
            stage = status.get("status")
            info = status.get("info_dict") or {}
            filename = status.get("filename") or info.get("filepath") or info.get("_filename") or info.get("title") or "item"
            name = Path(str(filename)).name

            if stage == "downloading":
                downloaded = status.get("downloaded_bytes")
                total = status.get("total_bytes") or status.get("total_bytes_estimate")
                speed = format_bytes(status.get("speed"))
                eta = format_seconds(status.get("eta"))
                if downloaded is not None and total:
                    percent = (float(downloaded) / float(total)) * 100.0
                    text = f"Downloading {name} — {percent:.1f}% ({format_bytes(downloaded)} / {format_bytes(total)}) at {speed}/s, ETA {eta}"
                    self.queue_progress(percent, text, indeterminate=False)
                else:
                    self.queue_progress(None, f"Downloading {name}...", indeterminate=True)
            elif stage == "finished":
                self.queue_log(f"✔ Finished: {name}")
                self.queue_progress(100, f"Finished: {name}", indeterminate=False)
            elif stage == "error":
                self.queue_log(f"ERROR while downloading: {name}")
        return hook

    def download_worker(self, config: dict) -> None:
        logger = self.queue_log

        try:
            logger("=" * 70)
            logger(" Smart YouTube Downloader GUI (macOS) ")
            logger("=" * 70)
            logger(f"Current output directory: {config['output_dir']}")
            logger("")

            check_ffmpeg(logger)
            check_js_runtime(logger)
            check_yt_dlp_version(logger)

            mode = config["mode"]
            media_type = config["media_type"]
            url = config["url"]

            if mode == "single":
                normalized = normalize_youtube_watch_url(url)
                if normalized != url:
                    logger(f"> Normalized video URL to:\n  {normalized}")
                url = normalized

            cookiesfrombrowser = None
            if config["use_cookies"]:
                cookiesfrombrowser = (config["browser"], None, None, None)
                logger(f"> Will use cookies from your '{config['browser']}' profile (you must be logged into YouTube there).")

            want_subs = bool(config["want_subs"])
            subs_langs: list[str] | None = None
            if want_subs:
                subs_lang = config["subs_lang"]
                subs_langs = [subs_lang]
                if media_type == "srt":
                    logger(f"> Subtitle-only mode: downloading subtitles only for language '{subs_lang}'.")
                    logger(f"> Subtitle strategy: try MANUAL '{subs_lang}' first, then AUTO-generated '{subs_lang}' only if manual is missing.")
                else:
                    logger(f"> Subtitle strategy: try MANUAL '{subs_lang}' first, then AUTO-generated '{subs_lang}' only if manual is missing.")

            if media_type == "video":
                format_str = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
            elif media_type == "audio":
                # Prefer normal audio-only formats, but fall back to any playable
                # format that contains audio. FFmpegExtractAudio below still
                # converts the result to MP3 ~192 kbps. This avoids failures like:
                #   Requested format is not available
                # when YouTube exposes only limited/muxed/HLS formats for an item.
                format_str = "bestaudio/best*[acodec!=none]/best"
            else:
                format_str = None

            extractor_args = build_youtube_extractor_args(logger)

            before_files = snapshot_all_files(config["output_dir"])

            ytdlp_logger = GuiLogger(logger)
            common_opts: dict = {
                "continuedl": True,
                "retries": 10,
                "fragment_retries": 10,
                "concurrent_fragment_downloads": 4,
                "progress_hooks": [self.make_progress_hook()],
                "windowsfilenames": True,
                "logger": ytdlp_logger,
                "paths": {"home": config["output_dir"]},
                "keepvideo": False,
            }
            if extractor_args:
                common_opts["extractor_args"] = extractor_args
            if format_str:
                common_opts["format"] = format_str

            js_runtime_opts = build_js_runtime_opts(logger)
            if js_runtime_opts:
                common_opts["js_runtimes"] = js_runtime_opts

            if media_type == "video":
                common_opts["merge_output_format"] = "mp4"
            elif media_type == "audio":
                common_opts["postprocessors"] = [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ]

            if cookiesfrombrowser is not None:
                common_opts["cookiesfrombrowser"] = cookiesfrombrowser

            if want_subs and subs_langs:
                common_opts.update(build_subtitle_opts(langs=subs_langs, auto=False, skip_download=(media_type == "srt")))

            if mode == "single":
                if media_type == "video":
                    outtmpl = "%(title)s.mp4"
                    mode_label = "VIDEO"
                    artifact_label = "video"
                elif media_type == "audio":
                    outtmpl = "%(title)s.%(ext)s"
                    mode_label = "AUDIO-ONLY ~192 kbps MP3"
                    artifact_label = "audio (mp3)"
                else:
                    outtmpl = "%(title)s.%(ext)s"
                    mode_label = "SRT-ONLY"
                    artifact_label = ".srt subtitles"

                ydl_opts = {**common_opts, "ignoreerrors": False, "noplaylist": True, "outtmpl": outtmpl}

                logger(f"> Starting single download... (mode: {mode_label})")
                self.queue_progress(None, "Starting single download...", indeterminate=True)
                result = run_download([url], ydl_opts, retry_without_cookies=True, logger=logger)

                cleanup_stats = SubtitleCleanupStats()
                residue_stats = cleanup_new_residue_since(before_files, config["output_dir"], logger)
                if result != 0:
                    summary = "Download finished with errors."
                    if residue_stats.removed_files:
                        summary += f" Residue cleanup removed {residue_stats.removed_files} file(s)."
                    self.queue.put(("done", False, summary))
                    return

                if want_subs and subs_langs:
                    cleanup_stats = run_optional_auto_sub_fallback(
                        [url],
                        ydl_opts,
                        output_dir=config["output_dir"],
                        retry_without_cookies=True,
                        logger=logger,
                    )

                if media_type == "srt":
                    summary = "All done. Check the output folder for the downloaded .srt subtitles only."
                else:
                    summary = f"All done. Check the output folder for the downloaded {artifact_label}{' and .srt subtitles' if want_subs else ''}."
                if want_subs and subs_langs and cleanup_stats.adjusted_files:
                    summary += f" AUTO-generated subtitle cleanup adjusted {cleanup_stats.adjusted_files} file(s) with {cleanup_stats.changed_cues} cue fix(es)."
                if residue_stats.removed_files:
                    summary += f" Residue cleanup removed {residue_stats.removed_files} file(s)."
                logger(summary)
                self.queue.put(("done", True, summary))
                return

            logger("> Playlist mode selected.")
            start_idx = parse_positive_int(config["start_idx"], "start index", logger)
            end_idx = parse_positive_int(config["end_idx"], "end index", logger)
            if start_idx and end_idx and end_idx < start_idx:
                logger(f"End index {end_idx} is less than start index {start_idx}, swapping.")
                start_idx, end_idx = end_idx, start_idx

            playlist_len = maybe_get_playlist_length(url, extractor_args, cookiesfrombrowser, logger)
            index_width = 2 if playlist_len is None or playlist_len < 100 else 3
            index_pattern = f"%(playlist_index)0{index_width}d"

            if media_type == "video":
                outtmpl = f"{index_pattern} - %(title)s.mp4"
                pattern_desc = f"{index_pattern.replace('%(playlist_index)', 'N')} - <title>.mp4"
                mode_label = "VIDEO"
            elif media_type == "audio":
                outtmpl = f"{index_pattern} - %(title)s.%(ext)s"
                pattern_desc = f"{index_pattern.replace('%(playlist_index)', 'N')} - <title>.mp3"
                mode_label = "AUDIO-ONLY ~192 kbps MP3"
            else:
                outtmpl = f"{index_pattern} - %(title)s.%(ext)s"
                pattern_desc = f"{index_pattern.replace('%(playlist_index)', 'N')} - <title>.srt"
                mode_label = "SRT-ONLY"

            logger(f"> Playlist detected. Total items (approx): {playlist_len or 'unknown'}")
            logger(f"> File naming pattern: {pattern_desc}")

            ydl_opts = {**common_opts, "ignoreerrors": True, "noplaylist": False, "outtmpl": outtmpl}
            if start_idx is not None:
                ydl_opts["playliststart"] = start_idx
            if end_idx is not None:
                ydl_opts["playlistend"] = end_idx

            logger(f"> Starting playlist download... (mode: {mode_label})")
            self.queue_progress(None, "Starting playlist download...", indeterminate=True)
            result = run_download([url], ydl_opts, retry_without_cookies=True, logger=logger)

            cleanup_stats = SubtitleCleanupStats()
            residue_stats = cleanup_new_residue_since(before_files, config["output_dir"], logger)
            if want_subs and subs_langs:
                cleanup_stats = run_optional_auto_sub_fallback(
                    [url],
                    ydl_opts,
                    output_dir=config["output_dir"],
                    retry_without_cookies=True,
                    logger=logger,
                )

            if result == 0:
                if media_type == "video":
                    summary = f"All done. Check the output folder for the downloaded episodes (video){' and .srt subtitles' if want_subs else ''}."
                elif media_type == "audio":
                    summary = f"All done. Check the output folder for the downloaded audio tracks (mp3){' and .srt subtitles' if want_subs else ''}."
                else:
                    summary = "All done. Check the output folder for the downloaded .srt subtitles only."
                if want_subs and subs_langs and cleanup_stats.adjusted_files:
                    summary += f" AUTO-generated subtitle cleanup adjusted {cleanup_stats.adjusted_files} file(s) with {cleanup_stats.changed_cues} cue fix(es)."
                if residue_stats.removed_files:
                    summary += f" Residue cleanup removed {residue_stats.removed_files} file(s)."
                logger(summary)
                self.queue.put(("done", True, summary))
            else:
                summary = "Finished, but yt-dlp reported one or more failures. Some items may have downloaded, and some may have been skipped."
                if residue_stats.removed_files:
                    summary += f" Residue cleanup removed {residue_stats.removed_files} file(s)."
                logger(summary)
                self.queue.put(("done", False, summary))

        except Exception as exc:
            logger(f"Unhandled error: {exc}")
            self.queue.put(("done", False, "Download failed."))
            self.queue.put(("errorbox", "Download failed", str(exc)))


def main() -> None:
    root = tk.Tk(className="SmartYTDownloaderMac")
    try:
        root.call("tk", "scaling", 1.15)
    except Exception:
        pass
    DownloaderGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
