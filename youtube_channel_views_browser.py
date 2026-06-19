#!/usr/bin/env python3
"""
Fetch videos from configured YouTube channels, sort them by view count, and
open a local HTML report in the browser.

The script installs its own Python metadata dependencies on first run if they
are missing:
    yt-dlp, yt-dlp-ejs

Usage:
    python3 youtube_channel_views_browser.py
    python3 youtube_channel_views_browser.py --cli --limit 50
    python3 youtube_channel_views_browser.py --cli --config other_config.json --no-open
"""

from __future__ import annotations

import argparse
import html
import json
import os
import queue
import re
import subprocess
import sys
import threading
import webbrowser
from dataclasses import dataclass, replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse


try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except AttributeError:
    pass


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "youtube_channel_views_config.json"
DEFAULT_OUTPUT_PATH = SCRIPT_DIR / "youtube_channel_views_report.html"
DEFAULT_HIDDEN_VIDEOS_FILENAME = "youtube_channel_views_hidden_videos.json"
DEFAULT_HIDDEN_VIDEOS_PATH = SCRIPT_DIR / DEFAULT_HIDDEN_VIDEOS_FILENAME
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "YouTubeChannelViewsBrowser"
VENV_DIR = APP_SUPPORT_DIR / "venv"
VENV_PYTHON = VENV_DIR / "bin" / "python"
DEPENDENCY_PACKAGES = ("yt-dlp", "yt-dlp-ejs")
SUPPORTED_COOKIE_BROWSERS = {"brave", "chrome", "chromium", "edge", "firefox", "opera", "safari", "vivaldi", "whale"}

DEFAULT_CONFIG = {
    "min_views": "50k",
    "max_videos_per_channel": None,
    "cookies_from_browser": "chrome",
    "browser_profile": None,
    "cookies_file": None,
    "hidden_videos_file": DEFAULT_HIDDEN_VIDEOS_FILENAME,
    "channels": [
        {"enabled": True, "url": "https://www.youtube.com/@audiobookbelaraby/videos"},
        {"enabled": True, "url": "https://www.youtube.com/@EslamAdel/videos"},
        {"enabled": True, "url": "https://www.youtube.com/@arabinglish/videos"},
        {
            "enabled": True,
            "url": "https://www.youtube.com/@%D9%85%D8%AC%D9%84%D8%A9-%D8%A7%D9%84%D9%83%D8%AA%D8%A8-%D8%A7%D9%84%D8%B5%D9%88%D8%AA%D9%8A%D8%A9/videos",
        },
        {"enabled": True, "url": "https://www.youtube.com/@PsalmsOfMeem/videos"},
    ],
}

COUNT_UNITS = {
    "": 1,
    "k": 1_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
}

YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
LogFn = Callable[[str], None]


@dataclass(frozen=True)
class ChannelConfig:
    url: str
    enabled: bool = True


@dataclass(frozen=True)
class Config:
    min_views: int
    channels: list[ChannelConfig]
    max_videos_per_channel: int | None = None
    fetch_missing_view_counts: bool = True
    open_browser: bool = True
    cookies_from_browser: str | None = None
    browser_profile: str | None = None
    browser_keyring: str | None = None
    browser_container: str | None = None
    cookies_file: Path | None = None
    hidden_videos_file: Path = DEFAULT_HIDDEN_VIDEOS_PATH
    config_dir: Path = SCRIPT_DIR


@dataclass(frozen=True)
class Video:
    video_id: str
    title: str
    url: str
    view_count: int
    channel: str
    channel_url: str
    duration: str
    published: str
    thumbnail_url: str


@dataclass(frozen=True)
class ChannelStats:
    channel_url: str
    title: str
    scanned: int = 0
    included: int = 0
    missing_view_count: int = 0
    detail_lookup_failed: int = 0
    auth_failed: int = 0
    error: str = ""


@dataclass(frozen=True)
class DetailFetchStats:
    attempted: int = 0
    failed: int = 0
    auth_failed: int = 0
    last_error: str = ""


class QuietYtdlpLogger:
    def debug(self, message: str) -> None:
        return

    def info(self, message: str) -> None:
        return

    def warning(self, message: str) -> None:
        return

    def error(self, message: str) -> None:
        return


def parse_count(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"Not a valid count: {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"Count must be zero or higher: {value!r}")
        return value
    if isinstance(value, float):
        if value < 0:
            raise ValueError(f"Count must be zero or higher: {value!r}")
        return int(value)
    if not isinstance(value, str):
        raise ValueError(f"Not a valid count: {value!r}")

    normalized = value.strip().lower().replace(",", "").replace("_", "")
    normalized = normalized.removesuffix("views").strip()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([kmb]?)", normalized)
    if not match:
        raise ValueError(f"Not a valid count: {value!r}. Use examples like 50000, 50k, or 1.2m.")

    number = float(match.group(1))
    multiplier = COUNT_UNITS[match.group(2)]
    return int(number * multiplier)


def optional_positive_int(value: Any, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive integer or null.") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} must be a positive integer or null.")
    return parsed


def optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "false"}:
        return None
    return text


def optional_path(value: Any, config_dir: Path) -> Path | None:
    text = optional_string(value)
    if text is None:
        return None

    path = Path(text).expanduser()
    if not path.is_absolute():
        path = config_dir / path
    return path


def optional_cookie_file(value: Any, config_dir: Path) -> Path | None:
    return optional_path(value, config_dir)


def path_text_relative_to(path: Path, base_dir: Path) -> str:
    path = path.expanduser()
    if not path.is_absolute():
        return str(path)

    try:
        return os.path.relpath(
            path.resolve(strict=False),
            base_dir.expanduser().resolve(strict=False),
        )
    except (OSError, ValueError):
        return str(path)


def optional_cookie_browser(value: Any) -> str | None:
    text = optional_string(value)
    if text is None:
        return None

    browser = text.lower()
    if browser not in SUPPORTED_COOKIE_BROWSERS:
        supported = ", ".join(sorted(SUPPORTED_COOKIE_BROWSERS))
        raise ValueError(f"cookies_from_browser must be one of: {supported}.")
    return browser


def optional_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled"}:
        return False
    return default


def parse_channel_config(value: Any, index: int) -> ChannelConfig:
    if isinstance(value, str):
        url = value.strip()
        enabled = True
    elif isinstance(value, dict):
        raw_url = value.get("url") or value.get("channel") or value.get("channel_url")
        url = str(raw_url or "").strip()
        enabled = optional_bool(value.get("enabled"), True)
    else:
        raise ValueError(f"channels[{index}] must be a URL string or an object with url/enabled.")

    if not url:
        raise ValueError(f"channels[{index}] has an empty URL.")
    return ChannelConfig(url=url, enabled=enabled)


def enabled_channel_urls(config: Config) -> list[str]:
    return [channel.url for channel in config.channels if channel.enabled]


def channel_config_to_json(channel: ChannelConfig) -> dict[str, Any]:
    return {
        "enabled": channel.enabled,
        "url": channel.url,
    }


def load_hidden_video_records(path: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}

    if isinstance(raw, dict) and isinstance(raw.get("videos"), dict):
        source = raw["videos"]
    elif isinstance(raw, dict):
        source = raw
    elif isinstance(raw, list):
        source = {
            str(item.get("video_id") or item.get("id") or video_id_from_url(str(item.get("url") or ""))): item
            for item in raw
            if isinstance(item, dict)
        }
    else:
        return {}

    records: dict[str, dict[str, Any]] = {}
    for key, value in source.items():
        if not isinstance(value, dict):
            continue
        video_id = str(value.get("video_id") or value.get("id") or key).strip()
        if not video_id:
            continue
        record = dict(value)
        record["video_id"] = video_id
        records[video_id] = record
    return records


def write_hidden_video_records(path: Path, records: dict[str, dict[str, Any]]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "videos": records,
    }
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def normalize_hidden_video_record(data: dict[str, Any]) -> dict[str, Any]:
    video_id = str(data.get("video_id") or data.get("id") or "").strip()
    url = str(data.get("url") or "").strip()
    if not video_id:
        video_id = video_id_from_url(url)
    if not video_id:
        raise ValueError("Missing video_id or URL.")

    return {
        "video_id": video_id,
        "title": str(data.get("title") or "").strip(),
        "url": url,
        "channel": str(data.get("channel") or "").strip(),
        "hidden_at": datetime.now().isoformat(timespec="seconds"),
    }


class HideVideoRequestHandler(BaseHTTPRequestHandler):
    server_version = "YouTubeChannelViewsHideServer/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/hide-video":
            self.send_json(404, {"ok": False, "error": "Not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw_body) if raw_body else {}
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object.")
            record = normalize_hidden_video_record(data)
            hidden_file = Path(getattr(self.server, "hidden_videos_file"))
            records = load_hidden_video_records(hidden_file)
            records[record["video_id"]] = record
            write_hidden_video_records(hidden_file, records)
        except Exception as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
            return

        self.send_json(
            200,
            {
                "ok": True,
                "video_id": record["video_id"],
                "hidden_videos_file": path_text_relative_to(hidden_file, SCRIPT_DIR),
                "count": len(records),
            },
        )


def start_hide_video_server(hidden_videos_file: Path) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), HideVideoRequestHandler)
    server.hidden_videos_file = hidden_videos_file.expanduser()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/hide-video"


def ensure_default_config(path: Path) -> None:
    if path.exists():
        return
    path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Created default config: {path}")


def load_config(path: Path) -> Config:
    path = path.expanduser()
    ensure_default_config(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Config is not valid JSON: {path}\n{exc}") from exc

    try:
        min_views = parse_count(raw.get("min_views", DEFAULT_CONFIG["min_views"]))
        raw_channels = raw.get("channels", [])
        if not isinstance(raw_channels, list):
            raise ValueError("channels must be a JSON list.")
        channels = [
            parse_channel_config(channel, index)
            for index, channel in enumerate(raw_channels)
            if not (isinstance(channel, str) and not channel.strip())
        ]
        if not channels:
            raise ValueError("channels cannot be empty.")
        max_videos_per_channel = optional_positive_int(
            raw.get("max_videos_per_channel"),
            "max_videos_per_channel",
        )
        fetch_missing_view_counts = bool(raw.get("fetch_missing_view_counts", True))
        open_browser = bool(raw.get("open_browser", True))
        cookies_from_browser = optional_cookie_browser(raw.get("cookies_from_browser"))
        browser_profile = optional_string(raw.get("browser_profile"))
        browser_keyring = optional_string(raw.get("browser_keyring"))
        browser_container = optional_string(raw.get("browser_container"))
        cookies_file = optional_cookie_file(raw.get("cookies_file"), path.parent)
        hidden_videos_file = optional_path(
            raw.get("hidden_videos_file", DEFAULT_CONFIG["hidden_videos_file"]),
            path.parent,
        ) or (path.parent / DEFAULT_HIDDEN_VIDEOS_FILENAME)
        if cookies_from_browser and cookies_file:
            raise ValueError("Use either cookies_from_browser or cookies_file, not both.")
    except ValueError as exc:
        raise SystemExit(f"Config error in {path}: {exc}") from exc

    return Config(
        min_views=min_views,
        channels=channels,
        max_videos_per_channel=max_videos_per_channel,
        fetch_missing_view_counts=fetch_missing_view_counts,
        open_browser=open_browser,
        cookies_from_browser=cookies_from_browser,
        browser_profile=browser_profile,
        browser_keyring=browser_keyring,
        browser_container=browser_container,
        cookies_file=cookies_file,
        hidden_videos_file=hidden_videos_file,
        config_dir=path.parent,
    )


def running_in_private_venv() -> bool:
    try:
        return Path(sys.prefix).resolve() == VENV_DIR.resolve()
    except OSError:
        return False


def run_command(command: list[str], failure_message: str) -> None:
    print("> " + " ".join(command))
    proc = subprocess.run(command, text=True, capture_output=True)
    if proc.returncode == 0:
        if proc.stdout.strip():
            print(proc.stdout.strip())
        return

    error = proc.stderr.strip() or proc.stdout.strip() or f"command exited with code {proc.returncode}"
    raise SystemExit(f"{failure_message}\n\n{error}")


def ensure_private_venv() -> None:
    APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    if VENV_PYTHON.exists():
        return

    print(f"Creating private Python environment:\n  {VENV_DIR}")
    run_command(
        [sys.executable, "-m", "venv", str(VENV_DIR)],
        "Could not create the private Python environment.",
    )


def restart_in_private_venv() -> None:
    if running_in_private_venv():
        return

    print(f"Restarting inside the private Python environment:\n  {VENV_PYTHON}")
    sys.stdout.flush()
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])


def install_dependencies() -> None:
    print("Installing/updating required Python packages:")
    print("  " + ", ".join(DEPENDENCY_PACKAGES))

    ensure_private_venv()
    run_command(
        [str(VENV_PYTHON), "-m", "pip", "install", "-U", "pip"],
        "Could not update pip inside the private Python environment.",
    )
    run_command(
        [str(VENV_PYTHON), "-m", "pip", "install", "-U", *DEPENDENCY_PACKAGES],
        "Could not install the required Python packages automatically.",
    )
    print("Dependency install completed.")
    restart_in_private_venv()


def import_youtube_dl(*, auto_install: bool = True):
    try:
        from yt_dlp import YoutubeDL  # type: ignore
    except ImportError as exc:
        if not auto_install:
            raise SystemExit(
                "yt-dlp is required for YouTube metadata.\n\n"
                "Run without --no-auto-install to let this script create its private environment, "
                "or install dependencies manually in your active Python."
            ) from exc

        if not running_in_private_venv() and VENV_PYTHON.exists():
            restart_in_private_venv()

        install_dependencies()
        try:
            from yt_dlp import YoutubeDL  # type: ignore
        except ImportError as second_exc:
            raise SystemExit(
                "The dependency install finished, but Python still cannot import yt-dlp.\n\n"
                f"Private environment: {VENV_DIR}"
            ) from second_exc
    return YoutubeDL


def ytdlp_options(config: Config, *, flat: bool, ignore_errors: bool = True) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": ignore_errors,
        "socket_timeout": 30,
        "retries": 3,
        "logger": QuietYtdlpLogger(),
    }
    if config.cookies_file is not None:
        options["cookiefile"] = str(config.cookies_file)
    elif config.cookies_from_browser is not None:
        options["cookiesfrombrowser"] = (
            config.cookies_from_browser,
            config.browser_profile,
            config.browser_keyring,
            config.browser_container,
        )

    if flat:
        options["extract_flat"] = "in_playlist"
        options["lazy_playlist"] = False
        if config.max_videos_per_channel is not None:
            options["playlistend"] = config.max_videos_per_channel
    return options


def coerce_optional_count(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return parse_count(value)
    except ValueError:
        return None


def video_url_from_entry(entry: dict[str, Any]) -> str:
    for key in ("webpage_url", "original_url", "url"):
        value = entry.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value

    video_id = entry.get("id") or entry.get("url")
    if isinstance(video_id, str) and YOUTUBE_VIDEO_ID_RE.fullmatch(video_id):
        return f"https://www.youtube.com/watch?v={video_id}"
    return ""


def video_id_from_url(url: str) -> str:
    if not url:
        return ""

    try:
        parsed = urlparse(url)
    except Exception:
        parsed = None

    if parsed is not None:
        host = parsed.netloc.lower()
        if host.endswith("youtu.be"):
            candidate = parsed.path.strip("/").split("/")[0]
            if YOUTUBE_VIDEO_ID_RE.fullmatch(candidate):
                return candidate
        if "youtube.com" in host:
            query_id = parse_qs(parsed.query).get("v", [""])[0]
            if YOUTUBE_VIDEO_ID_RE.fullmatch(query_id):
                return query_id
            parts = [part for part in parsed.path.split("/") if part]
            for marker in ("shorts", "embed", "live"):
                if marker in parts:
                    marker_index = parts.index(marker)
                    if marker_index + 1 < len(parts):
                        candidate = parts[marker_index + 1]
                        if YOUTUBE_VIDEO_ID_RE.fullmatch(candidate):
                            return candidate

    match = re.search(r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])", url)
    return match.group(1) if match else ""


def video_id_from_entry(entry: dict[str, Any]) -> str:
    raw_id = entry.get("id")
    if isinstance(raw_id, str) and YOUTUBE_VIDEO_ID_RE.fullmatch(raw_id):
        return raw_id
    return video_id_from_url(video_url_from_entry(entry))


def best_thumbnail_url(entry: dict[str, Any]) -> str:
    thumbnails = entry.get("thumbnails")
    if isinstance(thumbnails, list):
        valid = [thumb for thumb in thumbnails if isinstance(thumb, dict) and thumb.get("url")]
        if valid:
            best = max(valid, key=lambda thumb: int(thumb.get("width") or 0) * int(thumb.get("height") or 0))
            return str(best["url"])

    thumbnail = entry.get("thumbnail")
    return str(thumbnail) if thumbnail else ""


def format_duration(entry: dict[str, Any]) -> str:
    duration_string = entry.get("duration_string")
    if isinstance(duration_string, str) and duration_string.strip():
        return duration_string.strip()

    duration = entry.get("duration")
    if not isinstance(duration, (int, float)):
        return ""
    seconds = int(duration)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_published(entry: dict[str, Any]) -> str:
    upload_date = entry.get("upload_date")
    if isinstance(upload_date, str) and re.fullmatch(r"\d{8}", upload_date):
        return f"{upload_date[0:4]}-{upload_date[4:6]}-{upload_date[6:8]}"

    timestamp = entry.get("timestamp")
    if isinstance(timestamp, (int, float)):
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
    return ""


def video_from_entry(entry: dict[str, Any], channel_title: str, channel_url: str) -> Video | None:
    view_count = coerce_optional_count(entry.get("view_count"))
    if view_count is None:
        return None

    title = str(entry.get("title") or "Untitled video")
    url = video_url_from_entry(entry)
    channel = str(entry.get("channel") or entry.get("uploader") or channel_title or "Unknown channel")
    entry_channel_url = str(entry.get("channel_url") or entry.get("uploader_url") or channel_url)
    video_id = video_id_from_entry(entry) or url

    return Video(
        video_id=video_id,
        title=title,
        url=url,
        view_count=view_count,
        channel=channel,
        channel_url=entry_channel_url,
        duration=format_duration(entry),
        published=format_published(entry),
        thumbnail_url=best_thumbnail_url(entry),
    )


def merge_metadata(flat_entry: dict[str, Any], detail_entry: dict[str, Any]) -> dict[str, Any]:
    merged = dict(flat_entry)
    for key, value in detail_entry.items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def looks_like_auth_error(message: str) -> bool:
    lowered = message.lower()
    markers = (
        "sign in to confirm",
        "not a bot",
        "use --cookies-from-browser",
        "use --cookies",
        "login required",
        "cookies",
    )
    return any(marker in lowered for marker in markers)


def fetch_detail_for_missing_views(
    YoutubeDL: type,
    entries: list[dict[str, Any]],
    config: Config,
    logger: LogFn = print,
) -> tuple[list[dict[str, Any]], DetailFetchStats]:
    if not entries or not config.fetch_missing_view_counts:
        return entries, DetailFetchStats(attempted=0)

    resolved: list[dict[str, Any]] = []
    failed = 0
    auth_failed = 0
    last_error = ""
    options = ytdlp_options(config, flat=False, ignore_errors=False)
    with YoutubeDL(options) as ydl:
        for index, entry in enumerate(entries, start=1):
            url = video_url_from_entry(entry)
            if not url:
                resolved.append(entry)
                continue
            if index == 1 or index % 10 == 0 or index == len(entries):
                logger(f"  Fetching detailed view counts for entries missing counts... {index}/{len(entries)}")
            try:
                detail = ydl.extract_info(url, download=False, process=False)
            except Exception as exc:
                failed += 1
                message = str(exc).strip() or exc.__class__.__name__
                last_error = message
                if looks_like_auth_error(message):
                    auth_failed += 1
                resolved.append(entry)
                continue
            if isinstance(detail, dict):
                resolved.append(merge_metadata(entry, detail))
            else:
                failed += 1
                resolved.append(entry)
    return resolved, DetailFetchStats(
        attempted=len(entries),
        failed=failed,
        auth_failed=auth_failed,
        last_error=last_error,
    )


def fetch_channel_videos(
    YoutubeDL: type,
    channel_url: str,
    config: Config,
    logger: LogFn = print,
) -> tuple[list[Video], ChannelStats]:
    with YoutubeDL(ytdlp_options(config, flat=True)) as ydl:
        info = ydl.extract_info(channel_url, download=False)

    if not isinstance(info, dict):
        return [], ChannelStats(channel_url=channel_url, title=channel_url, error="No channel metadata returned.")

    channel_title = str(info.get("channel") or info.get("uploader") or info.get("title") or channel_url)
    raw_entries = info.get("entries") or []
    entries = [entry for entry in raw_entries if isinstance(entry, dict)]
    missing_view_indexes = [
        index for index, entry in enumerate(entries)
        if coerce_optional_count(entry.get("view_count")) is None
    ]
    missing_view_entries = [entries[index] for index in missing_view_indexes]
    detail_stats = DetailFetchStats(attempted=len(missing_view_entries))
    if missing_view_entries and config.fetch_missing_view_counts:
        logger(
            f"  Found {format_count(len(entries))} videos; "
            f"{format_count(len(missing_view_entries))} need detailed view-count lookup."
        )
        resolved_entries, detail_stats = fetch_detail_for_missing_views(YoutubeDL, missing_view_entries, config, logger)
        for index, resolved_entry in zip(missing_view_indexes, resolved_entries):
            entries[index] = resolved_entry
        if detail_stats.auth_failed:
            logger(
                f"  Auth/bot check blocked {format_count(detail_stats.auth_failed)} detail lookups. "
                "Using browser cookies is required for those videos."
            )
        elif detail_stats.failed:
            logger(f"  Detail lookup failed for {format_count(detail_stats.failed)} videos.")

    videos: list[Video] = []
    missing_view_count = 0
    for entry in entries:
        video = video_from_entry(entry, channel_title, channel_url)
        if video is None:
            missing_view_count += 1
            continue
        if video.view_count >= config.min_views:
            videos.append(video)

    return videos, ChannelStats(
        channel_url=channel_url,
        title=channel_title,
        scanned=len(entries),
        included=len(videos),
        missing_view_count=missing_view_count,
        detail_lookup_failed=detail_stats.failed,
        auth_failed=detail_stats.auth_failed,
    )


def format_count(value: int) -> str:
    return f"{value:,}"


def escape(value: str) -> str:
    return html.escape(value or "", quote=True)


def render_channel_stats(stats: list[ChannelStats]) -> str:
    rows = []
    for stat in stats:
        title = escape(stat.title or stat.channel_url)
        url = escape(stat.channel_url)
        if stat.error:
            result = f"<span class=\"error\">{escape(stat.error)}</span>"
        else:
            parts = [
                f"{format_count(stat.included)} included / {format_count(stat.scanned)} scanned"
                f" / {format_count(stat.missing_view_count)} missing views",
            ]
            if stat.auth_failed:
                parts.append(f"<span class=\"error\">{format_count(stat.auth_failed)} blocked by YouTube auth/bot check</span>")
            elif stat.detail_lookup_failed:
                parts.append(f"{format_count(stat.detail_lookup_failed)} detail lookups failed")
            result = " / ".join(parts)
        rows.append(
            "<tr>"
            f"<td><a href=\"{url}\" target=\"_blank\" rel=\"noopener\">{title}</a></td>"
            f"<td>{result}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def render_video_rows(videos: list[Video], *, hide_endpoint: str | None = None) -> str:
    if not videos:
        return (
            "<tr>"
            "<td class=\"empty\" colspan=\"8\">No videos met the configured minimum view count.</td>"
            "</tr>"
        )

    rows = []
    for index, video in enumerate(videos, start=1):
        title = escape(video.title)
        url = escape(video.url)
        channel = escape(video.channel)
        channel_url = escape(video.channel_url)
        thumbnail = escape(video.thumbnail_url)
        video_id = escape(video.video_id)
        if hide_endpoint:
            action_html = "<button type=\"button\" class=\"hide-video\">Hide</button>"
        else:
            action_html = (
                "<button type=\"button\" class=\"hide-video\" disabled "
                "title=\"Open this report from the GUI to enable hiding\">Hide</button>"
            )
        thumbnail_html = (
            f"<a href=\"{url}\" target=\"_blank\" rel=\"noopener\"><img src=\"{thumbnail}\" alt=\"\"></a>"
            if thumbnail and url
            else ""
        )
        title_html = (
            f"<a class=\"video-title\" href=\"{url}\" target=\"_blank\" rel=\"noopener\" dir=\"auto\">{title}</a>"
            if url
            else f"<span class=\"video-title\" dir=\"auto\">{title}</span>"
        )
        channel_html = (
            f"<a href=\"{channel_url}\" target=\"_blank\" rel=\"noopener\" dir=\"auto\">{channel}</a>"
            if channel_url
            else f"<span dir=\"auto\">{channel}</span>"
        )
        rows.append(
            "<tr "
            f"data-video-id=\"{video_id}\" "
            f"data-video-title=\"{title}\" "
            f"data-video-url=\"{url}\" "
            f"data-video-channel=\"{channel}\">"
            f"<td class=\"rank\">{index}</td>"
            f"<td class=\"thumb\">{thumbnail_html}</td>"
            f"<td>{title_html}<div class=\"url\">{url}</div></td>"
            f"<td class=\"views\">{format_count(video.view_count)}</td>"
            f"<td>{channel_html}</td>"
            f"<td>{escape(video.published)}</td>"
            f"<td>{escape(video.duration)}</td>"
            f"<td class=\"actions\">{action_html}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def render_html(
    videos: list[Video],
    stats: list[ChannelStats],
    config: Config,
    *,
    hide_endpoint: str | None = None,
) -> str:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scanned_count = sum(stat.scanned for stat in stats)
    error_count = sum(1 for stat in stats if stat.error)
    hide_endpoint_js = json.dumps(hide_endpoint or "")
    hidden_file_text = escape(path_text_relative_to(config.hidden_videos_file, config.config_dir))

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>YouTube Channel Videos by Views</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #18212f;
      --muted: #667085;
      --border: #d9dee7;
      --accent: #0f766e;
      --accent-soft: #e5f3f1;
      --error: #b42318;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      background: var(--panel);
      border-bottom: 1px solid var(--border);
      padding: 26px 32px 22px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 26px;
      line-height: 1.2;
    }}
    p {{ margin: 0; color: var(--muted); }}
    main {{ padding: 24px 32px 40px; }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 12px;
      margin-bottom: 22px;
    }}
    .stat {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px 14px;
    }}
    .stat strong {{
      display: block;
      font-size: 21px;
      line-height: 1.2;
    }}
    .stat span {{ color: var(--muted); font-size: 13px; }}
    section {{
      margin-top: 22px;
    }}
    h2 {{
      margin: 0 0 10px;
      font-size: 18px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{
      border-bottom: 1px solid var(--border);
      padding: 10px 12px;
      text-align: left;
      vertical-align: middle;
    }}
    th {{
      position: sticky;
      top: 0;
      z-index: 1;
      background: #eef1f5;
      color: #344054;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .04em;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .rank {{ width: 54px; color: var(--muted); }}
    .thumb {{ width: 138px; }}
    .thumb img {{
      display: block;
      width: 120px;
      aspect-ratio: 16 / 9;
      object-fit: cover;
      border-radius: 6px;
      background: #e4e7ec;
    }}
    .video-title {{
      display: inline-block;
      color: var(--text);
      font-weight: 650;
      max-width: 760px;
    }}
    .views {{
      color: #0b4f4a;
      font-weight: 700;
      white-space: nowrap;
    }}
    .url {{
      margin-top: 3px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .actions {{
      width: 90px;
      white-space: nowrap;
    }}
    button {{
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #ffffff;
      color: var(--text);
      cursor: pointer;
      font: inherit;
      padding: 5px 9px;
    }}
    button:hover {{ border-color: var(--accent); color: var(--accent); }}
    .hidden-by-user {{ display: none; }}
    .report-controls {{
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: 10px 14px;
      margin: 0 0 12px;
    }}
    .report-controls code,
    #hide-status {{
      color: var(--muted);
      font-size: 13px;
    }}
    .error {{ color: var(--error); font-weight: 650; }}
    .empty {{
      color: var(--muted);
      padding: 26px 12px;
      text-align: center;
    }}
    @media (max-width: 820px) {{
      header, main {{ padding-left: 16px; padding-right: 16px; }}
      table, thead, tbody, tr, th, td {{ display: block; }}
      thead {{ display: none; }}
      tr {{ border-bottom: 1px solid var(--border); padding: 10px 0; }}
      td {{ border-bottom: 0; padding: 6px 12px; }}
      .rank, .thumb {{ width: auto; }}
      .thumb img {{ width: 100%; max-width: 260px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>YouTube Channel Videos by Views</h1>
    <p>Generated {escape(generated_at)}. Minimum views: {format_count(config.min_views)}.</p>
  </header>
  <main>
    <div class="stats">
      <div class="stat"><strong>{format_count(len(videos))}</strong><span>videos included</span></div>
      <div class="stat"><strong>{format_count(scanned_count)}</strong><span>videos scanned</span></div>
      <div class="stat"><strong>{format_count(len(stats))}</strong><span>channels configured</span></div>
      <div class="stat"><strong>{format_count(error_count)}</strong><span>channel errors</span></div>
    </div>

    <section>
      <h2>Videos</h2>
      <div class="report-controls">
        <span>Hidden-video file: <code>{hidden_file_text}</code></span>
        <span id="hide-status"></span>
      </div>
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Thumbnail</th>
            <th>Video</th>
            <th>Views</th>
            <th>Channel</th>
            <th>Published</th>
            <th>Duration</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          {render_video_rows(videos, hide_endpoint=hide_endpoint)}
        </tbody>
      </table>
    </section>

    <section>
      <h2>Channel Scan</h2>
      <table>
        <thead>
          <tr>
            <th>Channel</th>
            <th>Result</th>
          </tr>
        </thead>
        <tbody>
          {render_channel_stats(stats)}
        </tbody>
      </table>
    </section>
  </main>
  <script>
    (function () {{
      var hideEndpoint = {hide_endpoint_js};
      var hideStatus = document.getElementById("hide-status");

      function rowRecord(row) {{
        return {{
          video_id: row.dataset.videoId || "",
          title: row.dataset.videoTitle || "",
          url: row.dataset.videoUrl || "",
          channel: row.dataset.videoChannel || ""
        }};
      }}

      function setStatus(message) {{
        hideStatus.textContent = message || "";
      }}

      if (!hideEndpoint) {{
        setStatus("Open this report from the GUI to enable Hide buttons.");
      }}

      document.querySelectorAll(".hide-video").forEach(function (button) {{
        button.addEventListener("click", function () {{
          var row = button.closest("tr[data-video-id]");
          if (!row) {{
            return;
          }}
          var record = rowRecord(row);
          if (!record.video_id) {{
            return;
          }}
          if (!hideEndpoint) {{
            alert("Open this report from the GUI to enable hiding videos.");
            return;
          }}
          button.disabled = true;
          button.textContent = "Saving...";
          fetch(hideEndpoint, {{
            method: "POST",
            headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify(record)
          }})
            .then(function (response) {{
              return response.json().then(function (payload) {{
                if (!response.ok || !payload.ok) {{
                  throw new Error(payload.error || "Could not save hidden video.");
                }}
                return payload;
              }});
            }})
            .then(function (payload) {{
              row.classList.add("hidden-by-user");
              setStatus("Saved hidden video to external file. Hidden count: " + payload.count);
            }})
            .catch(function (error) {{
              button.disabled = false;
              button.textContent = "Hide";
              alert("Could not save hidden video: " + error.message);
            }});
        }});
      }});
    }})();
  </script>
</body>
</html>
"""


def apply_arg_overrides(config: Config, args: argparse.Namespace) -> Config:
    if args.limit is not None:
        if args.limit <= 0:
            raise SystemExit("--limit must be a positive integer.")
        config = replace(config, max_videos_per_channel=args.limit)
    if args.no_cookies:
        config = replace(
            config,
            cookies_from_browser=None,
            browser_profile=None,
            browser_keyring=None,
            browser_container=None,
            cookies_file=None,
        )
    if args.cookies_file is not None:
        config = replace(
            config,
            cookies_file=args.cookies_file.expanduser(),
            cookies_from_browser=None,
            browser_profile=None,
            browser_keyring=None,
            browser_container=None,
        )
    if args.cookies_from_browser is not None:
        config = replace(
            config,
            cookies_from_browser=args.cookies_from_browser,
            browser_profile=args.browser_profile,
            cookies_file=None,
        )
    if args.no_open:
        config = replace(config, open_browser=False)
    return config


def build_report(
    config: Config,
    output_path: Path,
    *,
    auto_install: bool = True,
    update_deps: bool = False,
    hide_endpoint: str | None = None,
    logger: LogFn = print,
) -> tuple[Path, list[Video], list[ChannelStats]]:
    if update_deps:
        install_dependencies()

    YoutubeDL = import_youtube_dl(auto_install=auto_install)

    logger(f"Minimum views: {format_count(config.min_views)}")
    if config.max_videos_per_channel is not None:
        logger(f"Per-channel fetch limit: {format_count(config.max_videos_per_channel)}")
    if config.cookies_file is not None:
        logger(f"Using cookies file: {config.cookies_file}")
    elif config.cookies_from_browser is not None:
        profile_note = f" profile={config.browser_profile}" if config.browser_profile else ""
        logger(f"Using cookies from browser: {config.cookies_from_browser}{profile_note}")
    else:
        logger("No cookies configured; YouTube may block detail lookups.")

    channel_urls = enabled_channel_urls(config)
    if not channel_urls:
        raise ValueError("No enabled channels. Enable at least one channel before fetching.")

    all_videos: list[Video] = []
    stats: list[ChannelStats] = []
    disabled_count = len(config.channels) - len(channel_urls)
    if disabled_count:
        logger(f"Skipping {format_count(disabled_count)} disabled channel(s).")

    for index, channel_url in enumerate(channel_urls, start=1):
        logger(f"[{index}/{len(channel_urls)}] Fetching {channel_url}")
        try:
            videos, channel_stats = fetch_channel_videos(YoutubeDL, channel_url, config, logger)
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            logger(f"  ERROR: {message}")
            stats.append(ChannelStats(channel_url=channel_url, title=channel_url, error=message))
            continue

        all_videos.extend(videos)
        stats.append(channel_stats)
        logger(
            "  "
            f"Included {format_count(channel_stats.included)} of {format_count(channel_stats.scanned)} "
            f"videos from {channel_stats.title}"
        )

    all_videos.sort(key=lambda video: video.view_count, reverse=True)
    hidden_records = load_hidden_video_records(config.hidden_videos_file)
    if hidden_records:
        before_count = len(all_videos)
        hidden_ids = set(hidden_records)
        all_videos = [video for video in all_videos if video.video_id not in hidden_ids]
        filtered_count = before_count - len(all_videos)
        if filtered_count:
            logger(
                f"Filtered out {format_count(filtered_count)} hidden video(s) from "
                f"{path_text_relative_to(config.hidden_videos_file, config.config_dir)}"
            )

    output_path = output_path.expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_html(all_videos, stats, config, hide_endpoint=hide_endpoint), encoding="utf-8")

    logger(f"Saved report: {output_path}")
    if config.open_browser:
        webbrowser.open(output_path.resolve().as_uri())
        logger("Opened report in browser.")

    return output_path, all_videos, stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a browser report of configured YouTube videos sorted by views.")
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Run immediately from the command line. Without this flag, the GUI opens and waits for a click.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Config JSON path. Default: {DEFAULT_CONFIG_PATH}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help=f"HTML output path. Default: {DEFAULT_OUTPUT_PATH}",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Temporarily limit the number of videos fetched per channel for this run.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Write the HTML report without opening it in the browser.",
    )
    parser.add_argument(
        "--update-deps",
        action="store_true",
        help="Update yt-dlp dependencies before fetching videos.",
    )
    parser.add_argument(
        "--no-auto-install",
        action="store_true",
        help="Do not auto-install missing yt-dlp dependencies.",
    )
    parser.add_argument(
        "--cookies-from-browser",
        choices=sorted(SUPPORTED_COOKIE_BROWSERS),
        default=None,
        help="Load YouTube cookies from a signed-in browser, e.g. safari, chrome, firefox, brave, or edge.",
    )
    parser.add_argument(
        "--browser-profile",
        default=None,
        help="Optional browser profile name/path for --cookies-from-browser.",
    )
    parser.add_argument(
        "--cookies-file",
        type=Path,
        default=None,
        help="Use a Netscape-format cookies.txt file instead of browser cookies.",
    )
    parser.add_argument(
        "--no-cookies",
        action="store_true",
        help="Disable config cookie settings for this run.",
    )
    return parser.parse_args()


def run_cli(args: argparse.Namespace) -> int:
    config = load_config(args.config.expanduser())
    config = apply_arg_overrides(config, args)
    build_report(
        config,
        args.output,
        auto_install=not args.no_auto_install,
        update_deps=args.update_deps,
    )
    return 0


def ensure_gui_runtime(args: argparse.Namespace) -> None:
    if args.no_auto_install or running_in_private_venv():
        return
    if VENV_PYTHON.exists():
        restart_in_private_venv()
    install_dependencies()


def launch_gui(args: argparse.Namespace) -> int:
    ensure_gui_runtime(args)

    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

    class YouTubeViewsApp:
        def __init__(self, root: tk.Tk) -> None:
            self.root = root
            self.root.title("YouTube Channel Views Browser")
            self.root.geometry("980x760")
            self.root.minsize(860, 620)

            self.config_path = args.config.expanduser()
            self.output_path = args.output.expanduser()
            self.worker: threading.Thread | None = None
            self.hide_server: ThreadingHTTPServer | None = None
            self.hide_endpoint: str | None = None
            self.messages: queue.Queue[tuple[str, Any]] = queue.Queue()

            self.config_var = tk.StringVar(value=str(self.config_path))
            self.output_var = tk.StringVar(value=str(self.output_path))
            self.hidden_file_var = tk.StringVar(value=DEFAULT_HIDDEN_VIDEOS_FILENAME)
            self.min_views_var = tk.StringVar(value="50k")
            self.max_videos_var = tk.StringVar(value="")
            self.cookies_browser_var = tk.StringVar(value="")
            self.browser_profile_var = tk.StringVar(value="")
            self.cookies_file_var = tk.StringVar(value="")
            self.open_browser_var = tk.BooleanVar(value=True)
            self.status_var = tk.StringVar(value="Ready. Click Fetch Videos to build the report.")

            self._build_ui()
            self.reload_config(show_status=False)
            self.root.protocol("WM_DELETE_WINDOW", self.on_close)
            self.root.after(100, self._drain_messages)

        def _build_ui(self) -> None:
            root = self.root
            root.columnconfigure(0, weight=1)
            root.rowconfigure(0, weight=1)

            outer = ttk.Frame(root, padding=14)
            outer.grid(row=0, column=0, sticky="nsew")
            outer.columnconfigure(0, weight=1)
            outer.rowconfigure(3, weight=1)
            outer.rowconfigure(5, weight=1)

            heading = ttk.Label(outer, text="YouTube Channel Views Browser", font=("TkDefaultFont", 18, "bold"))
            heading.grid(row=0, column=0, sticky="w", pady=(0, 12))

            files = ttk.Frame(outer)
            files.grid(row=1, column=0, sticky="ew", pady=(0, 10))
            files.columnconfigure(1, weight=1)

            ttk.Label(files, text="Config").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
            ttk.Entry(files, textvariable=self.config_var).grid(row=0, column=1, sticky="ew", pady=3)
            ttk.Button(files, text="Browse", command=self.browse_config).grid(row=0, column=2, padx=(8, 0), pady=3)
            ttk.Button(files, text="Open", command=self.open_config).grid(row=0, column=3, padx=(8, 0), pady=3)

            ttk.Label(files, text="Report").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
            ttk.Entry(files, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", pady=3)
            ttk.Button(files, text="Browse", command=self.browse_output).grid(row=1, column=2, padx=(8, 0), pady=3)
            ttk.Button(files, text="Open", command=self.open_report).grid(row=1, column=3, padx=(8, 0), pady=3)

            ttk.Label(files, text="Hidden list").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
            ttk.Entry(files, textvariable=self.hidden_file_var).grid(row=2, column=1, sticky="ew", pady=3)
            ttk.Button(files, text="Browse", command=self.browse_hidden_file).grid(row=2, column=2, padx=(8, 0), pady=3)
            ttk.Button(files, text="Open", command=self.open_hidden_file).grid(row=2, column=3, padx=(8, 0), pady=3)

            settings = ttk.Frame(outer)
            settings.grid(row=2, column=0, sticky="ew", pady=(0, 10))
            for col in (1, 3, 5):
                settings.columnconfigure(col, weight=1)

            ttk.Label(settings, text="Min views").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
            ttk.Entry(settings, textvariable=self.min_views_var, width=12).grid(row=0, column=1, sticky="ew", pady=3)
            ttk.Label(settings, text="Max/channel").grid(row=0, column=2, sticky="w", padx=(14, 8), pady=3)
            ttk.Entry(settings, textvariable=self.max_videos_var, width=12).grid(row=0, column=3, sticky="ew", pady=3)
            ttk.Checkbutton(settings, text="Open browser after fetch", variable=self.open_browser_var).grid(
                row=0, column=4, columnspan=2, sticky="w", padx=(14, 0), pady=3
            )

            ttk.Label(settings, text="Cookies browser").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
            cookie_values = ["", *sorted(SUPPORTED_COOKIE_BROWSERS)]
            ttk.Combobox(
                settings,
                textvariable=self.cookies_browser_var,
                values=cookie_values,
                state="readonly",
                width=16,
            ).grid(row=1, column=1, sticky="ew", pady=3)
            ttk.Label(settings, text="Profile").grid(row=1, column=2, sticky="w", padx=(14, 8), pady=3)
            ttk.Entry(settings, textvariable=self.browser_profile_var).grid(row=1, column=3, sticky="ew", pady=3)
            ttk.Label(settings, text="Cookies file").grid(row=1, column=4, sticky="w", padx=(14, 8), pady=3)
            cookie_file_frame = ttk.Frame(settings)
            cookie_file_frame.grid(row=1, column=5, sticky="ew", pady=3)
            cookie_file_frame.columnconfigure(0, weight=1)
            ttk.Entry(cookie_file_frame, textvariable=self.cookies_file_var).grid(row=0, column=0, sticky="ew")
            ttk.Button(cookie_file_frame, text="Browse", command=self.browse_cookies_file).grid(row=0, column=1, padx=(8, 0))

            channels_frame = ttk.LabelFrame(outer, text="Channels")
            channels_frame.grid(row=3, column=0, sticky="nsew", pady=(0, 10))
            channels_frame.columnconfigure(0, weight=1)
            channels_frame.rowconfigure(0, weight=1)
            self.channels_tree = ttk.Treeview(
                channels_frame,
                columns=("enabled", "url"),
                show="headings",
                selectmode="extended",
                height=8,
            )
            self.channels_tree.heading("enabled", text="Enabled")
            self.channels_tree.heading("url", text="Channel URL")
            self.channels_tree.column("enabled", width=90, minwidth=80, stretch=False, anchor="center")
            self.channels_tree.column("url", width=760, minwidth=260, stretch=True)
            self.channels_tree.tag_configure("disabled", foreground="#8a8f98")
            self.channels_tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
            self.channels_tree.bind("<Double-1>", self.toggle_selected_channels)

            channels_scroll = ttk.Scrollbar(channels_frame, orient="vertical", command=self.channels_tree.yview)
            channels_scroll.grid(row=0, column=1, sticky="ns", padx=(0, 8), pady=8)
            self.channels_tree.configure(yscrollcommand=channels_scroll.set)

            channel_buttons = ttk.Frame(channels_frame)
            channel_buttons.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
            ttk.Button(channel_buttons, text="Add", command=self.add_channel).pack(side="left")
            ttk.Button(channel_buttons, text="Edit", command=self.edit_channel).pack(side="left", padx=(8, 0))
            ttk.Button(channel_buttons, text="Remove", command=self.remove_selected_channels).pack(side="left", padx=(8, 0))
            ttk.Button(channel_buttons, text="Enable", command=lambda: self.set_selected_enabled(True)).pack(side="left", padx=(20, 0))
            ttk.Button(channel_buttons, text="Disable", command=lambda: self.set_selected_enabled(False)).pack(side="left", padx=(8, 0))
            ttk.Button(channel_buttons, text="Toggle", command=self.toggle_selected_channels).pack(side="left", padx=(8, 0))

            buttons = ttk.Frame(outer)
            buttons.grid(row=4, column=0, sticky="ew", pady=(0, 10))
            self.fetch_button = ttk.Button(buttons, text="Fetch Videos", command=self.start_fetch)
            self.fetch_button.pack(side="left")
            ttk.Button(buttons, text="Save Config", command=self.save_config).pack(side="left", padx=(8, 0))
            ttk.Button(buttons, text="Reload Config", command=self.reload_config).pack(side="left", padx=(8, 0))
            ttk.Button(buttons, text="Clear Log", command=self.clear_log).pack(side="right")

            log_frame = ttk.LabelFrame(outer, text="Log")
            log_frame.grid(row=5, column=0, sticky="nsew", pady=(0, 10))
            log_frame.columnconfigure(0, weight=1)
            log_frame.rowconfigure(0, weight=1)
            self.log_text = scrolledtext.ScrolledText(log_frame, height=12, wrap="word", state="disabled")
            self.log_text.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

            ttk.Label(outer, textvariable=self.status_var).grid(row=6, column=0, sticky="w")

        def browse_config(self) -> None:
            chosen = filedialog.askopenfilename(
                title="Choose config JSON",
                initialdir=str(self.config_path.parent),
                filetypes=[("JSON files", "*.json"), ("All files", "*")],
            )
            if chosen:
                self.config_path = Path(chosen).expanduser()
                self.config_var.set(str(self.config_path))
                self.reload_config()

        def browse_output(self) -> None:
            chosen = filedialog.asksaveasfilename(
                title="Choose report HTML",
                initialdir=str(self.output_path.parent),
                initialfile=self.output_path.name,
                defaultextension=".html",
                filetypes=[("HTML files", "*.html"), ("All files", "*")],
            )
            if chosen:
                self.output_path = Path(chosen).expanduser()
                self.output_var.set(str(self.output_path))

        def browse_hidden_file(self) -> None:
            config_dir = Path(self.config_var.get()).expanduser().parent
            path = optional_path(self.hidden_file_var.get(), config_dir) or (
                config_dir / DEFAULT_HIDDEN_VIDEOS_FILENAME
            )
            chosen = filedialog.asksaveasfilename(
                title="Choose hidden videos JSON",
                initialdir=str(path.parent),
                initialfile=path.name,
                defaultextension=".json",
                filetypes=[("JSON files", "*.json"), ("All files", "*")],
            )
            if chosen:
                self.hidden_file_var.set(path_text_relative_to(Path(chosen), config_dir))

        def browse_cookies_file(self) -> None:
            chosen = filedialog.askopenfilename(
                title="Choose cookies.txt",
                filetypes=[("Cookie files", "*.txt"), ("All files", "*")],
            )
            if chosen:
                config_dir = Path(self.config_var.get()).expanduser().parent
                self.cookies_file_var.set(path_text_relative_to(Path(chosen), config_dir))
                self.cookies_browser_var.set("")

        def open_config(self) -> None:
            path = Path(self.config_var.get()).expanduser()
            if not path.exists():
                messagebox.showwarning("Config not found", f"No file exists at:\n{path}")
                return
            subprocess.run(["open", str(path)], check=False)

        def open_report(self) -> None:
            path = Path(self.output_var.get()).expanduser()
            if not path.exists():
                messagebox.showwarning("Report not found", f"No report exists at:\n{path}")
                return
            webbrowser.open(path.resolve().as_uri())

        def open_hidden_file(self) -> None:
            config_dir = Path(self.config_var.get()).expanduser().parent
            path = optional_path(self.hidden_file_var.get(), config_dir) or (
                config_dir / DEFAULT_HIDDEN_VIDEOS_FILENAME
            )
            if not path.exists():
                write_hidden_video_records(path, {})
            subprocess.run(["open", str(path)], check=False)

        def ensure_hide_endpoint(self, hidden_videos_file: Path) -> str:
            hidden_videos_file = hidden_videos_file.expanduser()
            if self.hide_server is None:
                self.hide_server, self.hide_endpoint = start_hide_video_server(hidden_videos_file)
                self.append_log(f"Started hide-button helper: {self.hide_endpoint}")
            else:
                self.hide_server.hidden_videos_file = hidden_videos_file  # type: ignore[attr-defined]
            return self.hide_endpoint or ""

        def on_close(self) -> None:
            if self.hide_server is not None:
                self.hide_server.shutdown()
                self.hide_server.server_close()
            self.root.destroy()

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
            self.messages.put(("log", message))

        def enabled_label(self, enabled: bool) -> str:
            return "Yes" if enabled else "No"

        def insert_channel_row(self, channel: ChannelConfig) -> None:
            tags = () if channel.enabled else ("disabled",)
            self.channels_tree.insert("", "end", values=(self.enabled_label(channel.enabled), channel.url), tags=tags)

        def set_channels(self, channels: list[ChannelConfig]) -> None:
            self.channels_tree.delete(*self.channels_tree.get_children())
            for channel in channels:
                self.insert_channel_row(channel)

        def get_channels(self) -> list[ChannelConfig]:
            channels: list[ChannelConfig] = []
            for item_id in self.channels_tree.get_children():
                enabled_text, url = self.channels_tree.item(item_id, "values")
                channels.append(ChannelConfig(url=str(url).strip(), enabled=(str(enabled_text) == "Yes")))
            return channels

        def selected_channel_ids(self) -> tuple[str, ...]:
            selected = self.channels_tree.selection()
            if selected:
                return selected
            focused = self.channels_tree.focus()
            return (focused,) if focused else ()

        def add_channel(self) -> None:
            url = simpledialog.askstring("Add channel", "Channel URL:", parent=self.root)
            if not url:
                return
            url = url.strip()
            if not url:
                return
            self.insert_channel_row(ChannelConfig(url=url, enabled=True))

        def edit_channel(self) -> None:
            selected = self.selected_channel_ids()
            if len(selected) != 1:
                messagebox.showinfo("Edit channel", "Select exactly one channel to edit.")
                return
            item_id = selected[0]
            enabled_text, current_url = self.channels_tree.item(item_id, "values")
            url = simpledialog.askstring("Edit channel", "Channel URL:", initialvalue=current_url, parent=self.root)
            if not url:
                return
            channel = ChannelConfig(url=url.strip(), enabled=(str(enabled_text) == "Yes"))
            self.channels_tree.item(
                item_id,
                values=(self.enabled_label(channel.enabled), channel.url),
                tags=() if channel.enabled else ("disabled",),
            )

        def remove_selected_channels(self) -> None:
            selected = self.selected_channel_ids()
            if not selected:
                return
            if not messagebox.askyesno("Remove channels", f"Remove {len(selected)} selected channel(s)?"):
                return
            for item_id in selected:
                self.channels_tree.delete(item_id)

        def set_selected_enabled(self, enabled: bool) -> None:
            selected = self.selected_channel_ids()
            if not selected:
                return
            for item_id in selected:
                _, url = self.channels_tree.item(item_id, "values")
                self.channels_tree.item(
                    item_id,
                    values=(self.enabled_label(enabled), url),
                    tags=() if enabled else ("disabled",),
                )

        def toggle_selected_channels(self, event: object | None = None) -> None:
            selected = self.selected_channel_ids()
            if not selected:
                return
            for item_id in selected:
                enabled_text, url = self.channels_tree.item(item_id, "values")
                enabled = str(enabled_text) != "Yes"
                self.channels_tree.item(
                    item_id,
                    values=(self.enabled_label(enabled), url),
                    tags=() if enabled else ("disabled",),
                )

        def reload_config(self, show_status: bool = True) -> None:
            try:
                self.config_path = Path(self.config_var.get()).expanduser()
                config = apply_arg_overrides(load_config(self.config_path), args)
            except BaseException as exc:
                messagebox.showerror("Config error", str(exc))
                return

            self.output_path = Path(self.output_var.get()).expanduser()
            self.min_views_var.set(format_count(config.min_views).replace(",", ""))
            self.max_videos_var.set("" if config.max_videos_per_channel is None else str(config.max_videos_per_channel))
            self.cookies_browser_var.set(config.cookies_from_browser or "")
            self.browser_profile_var.set(config.browser_profile or "")
            self.cookies_file_var.set(
                "" if config.cookies_file is None else path_text_relative_to(config.cookies_file, config.config_dir)
            )
            self.hidden_file_var.set(path_text_relative_to(config.hidden_videos_file, config.config_dir))
            self.open_browser_var.set(config.open_browser)
            self.set_channels(config.channels)
            if show_status:
                self.status_var.set(f"Loaded config: {self.config_path}")

        def form_config(self, *, require_enabled: bool = False) -> Config:
            min_views_text = self.min_views_var.get().strip()
            max_videos_text = self.max_videos_var.get().strip()
            config_dir = Path(self.config_var.get()).expanduser().parent
            channels = [channel for channel in self.get_channels() if channel.url]
            if not channels:
                raise ValueError("Add at least one channel URL.")
            if require_enabled and not any(channel.enabled for channel in channels):
                raise ValueError("Enable at least one channel before fetching.")

            cookies_browser = optional_cookie_browser(self.cookies_browser_var.get())
            cookies_file = optional_cookie_file(self.cookies_file_var.get(), config_dir)
            hidden_videos_file = optional_path(
                self.hidden_file_var.get(),
                config_dir,
            ) or (config_dir / DEFAULT_HIDDEN_VIDEOS_FILENAME)
            if cookies_browser and cookies_file:
                raise ValueError("Use either a cookies browser or a cookies file, not both.")

            return Config(
                min_views=parse_count(min_views_text),
                channels=channels,
                max_videos_per_channel=optional_positive_int(max_videos_text, "Max videos/channel"),
                fetch_missing_view_counts=True,
                open_browser=bool(self.open_browser_var.get()),
                cookies_from_browser=cookies_browser,
                browser_profile=optional_string(self.browser_profile_var.get()),
                cookies_file=cookies_file,
                hidden_videos_file=hidden_videos_file,
                config_dir=config_dir,
            )

        def config_json(self, config: Config) -> dict[str, Any]:
            config_dir = Path(self.config_var.get()).expanduser().parent
            cookies_file = None
            if config.cookies_file is not None:
                cookies_file = path_text_relative_to(config.cookies_file, config_dir)

            return {
                "min_views": self.min_views_var.get().strip() or str(config.min_views),
                "max_videos_per_channel": config.max_videos_per_channel,
                "cookies_from_browser": config.cookies_from_browser,
                "browser_profile": config.browser_profile,
                "cookies_file": cookies_file,
                "hidden_videos_file": path_text_relative_to(config.hidden_videos_file, config_dir),
                "open_browser": config.open_browser,
                "channels": [channel_config_to_json(channel) for channel in config.channels],
            }

        def save_config(self) -> None:
            try:
                self.config_path = Path(self.config_var.get()).expanduser()
                config = self.form_config()
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                self.config_path.write_text(
                    json.dumps(self.config_json(config), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            except Exception as exc:
                messagebox.showerror("Save failed", str(exc))
                return
            self.status_var.set(f"Saved config: {self.config_path}")

        def start_fetch(self) -> None:
            if self.worker and self.worker.is_alive():
                return
            try:
                config = self.form_config(require_enabled=True)
                output_path = Path(self.output_var.get()).expanduser()
                hide_endpoint = self.ensure_hide_endpoint(config.hidden_videos_file)
            except Exception as exc:
                messagebox.showerror("Cannot fetch", str(exc))
                return

            self.clear_log()
            self.fetch_button.configure(state="disabled")
            self.status_var.set("Fetching videos...")
            self.append_log("Fetch started.")
            self.worker = threading.Thread(
                target=self._fetch_worker,
                args=(config, output_path, hide_endpoint),
                daemon=True,
            )
            self.worker.start()

        def _fetch_worker(self, config: Config, output_path: Path, hide_endpoint: str) -> None:
            try:
                report_path, videos, stats = build_report(
                    config,
                    output_path,
                    auto_install=not args.no_auto_install,
                    update_deps=args.update_deps,
                    hide_endpoint=hide_endpoint,
                    logger=self.queue_log,
                )
            except SystemExit as exc:
                self.messages.put(("error", str(exc)))
            except Exception as exc:
                self.messages.put(("error", str(exc)))
            else:
                scanned = sum(stat.scanned for stat in stats)
                self.messages.put(("done", report_path, len(videos), scanned))

        def _drain_messages(self) -> None:
            try:
                while True:
                    kind, *payload = self.messages.get_nowait()
                    if kind == "log":
                        self.append_log(str(payload[0]))
                    elif kind == "error":
                        self.fetch_button.configure(state="normal")
                        self.status_var.set("Fetch failed.")
                        self.append_log("ERROR: " + str(payload[0]))
                        messagebox.showerror("Fetch failed", str(payload[0]))
                    elif kind == "done":
                        report_path, included, scanned = payload
                        self.fetch_button.configure(state="normal")
                        self.status_var.set(f"Done. {included} videos included from {scanned} scanned.")
                        self.append_log(f"Done. Report: {report_path}")
            except queue.Empty:
                pass
            self.root.after(100, self._drain_messages)

    root = tk.Tk(className="YouTubeChannelViewsBrowser")
    YouTubeViewsApp(root)
    root.mainloop()
    return 0


def main() -> int:
    args = parse_args()
    if args.cli:
        return run_cli(args)
    return launch_gui(args)


if __name__ == "__main__":
    raise SystemExit(main())
