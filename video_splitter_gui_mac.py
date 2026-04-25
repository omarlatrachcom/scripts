#!/usr/bin/env python3
"""
Tkinter GUI for splitting local video files on macOS.

What it covers:
- Pick a folder, list videos, choose one
- Split mode 1: two-part cut at a chosen time (part1 / part2)
- Split mode 2: window split with overlap (vsplit / vsplit_srt style)
- Optionally split one or two external SRT files alongside the video
- Optionally convert split SRT outputs to ASS using styling compatible with the
  user's earlier subtitle GUI workflow

macOS notes:
- Requires ffmpeg + ffprobe on PATH (e.g. brew install ffmpeg)
- Pure stdlib GUI (tkinter)
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk, scrolledtext


# ----------------------------- App state ----------------------------- #

APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "VideoSplitGUI"
APP_STATE_FILE = APP_SUPPORT_DIR / "gui_state.json"
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".flv", ".ts")

PALETTE = {
    "window_bg": "#F5F5F7",
    "header_bg": "#ECECEF",
    "card_bg": "#FFFFFF",
    "field_bg": "#FFFFFF",
    "text": "#1D1D1F",
    "muted_text": "#6E6E73",
    "border": "#D2D2D7",
    "button_bg": "#FFFFFF",
    "button_hover": "#F2F2F2",
    "button_pressed": "#E5E5E7",
    "disabled_bg": "#F2F2F2",
    "disabled_text": "#9B9BA0",
    "accent": "#0A84FF",
    "accent_hover": "#3395FF",
    "accent_pressed": "#006BE6",
}


def ensure_state_dir() -> None:
    APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)


def load_gui_state() -> dict:
    try:
        return json.loads(APP_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_gui_state(data: dict) -> None:
    ensure_state_dir()
    APP_STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


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


def apply_theme(root: tk.Tk) -> dict:
    palette = dict(PALETTE)
    style = ttk.Style(root)
    try:
        style.theme_use("aqua")
    except tk.TclError:
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

    sans = _pick_first_available_font(root, ("SF Pro Text", "Helvetica Neue", "Helvetica", "Arial", "Inter"), "TkDefaultFont")
    mono = _pick_first_available_font(root, ("SF Mono", "Menlo", "Monaco", "Courier New"), "TkFixedFont")

    fonts = {
        "base": (sans, 12),
        "small": (sans, 11),
        "title": (sans, 18, "bold"),
        "subtitle": (sans, 11),
        "mono": (mono, 11),
    }

    root.configure(bg=palette["window_bg"])
    root.option_add("*tearOff", False)
    default_font = tkfont.nametofont("TkDefaultFont")
    default_font.configure(family=sans, size=12)

    style.configure(".", background=palette["window_bg"], foreground=palette["text"], font=fonts["base"])
    style.configure("App.TFrame", background=palette["window_bg"])
    style.configure("Header.TFrame", background=palette["header_bg"])
    style.configure("HeaderTitle.TLabel", background=palette["header_bg"], foreground=palette["text"], font=fonts["title"])
    style.configure("HeaderSubtitle.TLabel", background=palette["header_bg"], foreground=palette["muted_text"], font=fonts["subtitle"])
    style.configure("Card.TLabelframe", background=palette["card_bg"], relief="solid", borderwidth=1)
    style.configure("Card.TLabelframe.Label", background=palette["card_bg"], foreground=palette["text"], font=(sans, 12, "bold"))
    style.configure("Card.TFrame", background=palette["card_bg"])
    style.configure("Card.TLabel", background=palette["card_bg"], foreground=palette["text"], font=fonts["base"])
    style.configure("Muted.Card.TLabel", background=palette["card_bg"], foreground=palette["muted_text"], font=fonts["small"])
    style.configure("Card.TCheckbutton", background=palette["card_bg"], foreground=palette["text"], font=fonts["base"])
    style.configure("Card.TRadiobutton", background=palette["card_bg"], foreground=palette["text"], font=fonts["base"])
    style.map("Card.TCheckbutton", background=[("active", palette["card_bg"]), ("disabled", palette["card_bg"])], foreground=[("disabled", palette["disabled_text"])])
    style.map("Card.TRadiobutton", background=[("active", palette["card_bg"]), ("disabled", palette["card_bg"])], foreground=[("disabled", palette["disabled_text"])])
    style.configure("TButton", background=palette["button_bg"], foreground=palette["text"], borderwidth=1, padding=(12, 8), relief="flat", font=fonts["base"])
    style.map("TButton", background=[("disabled", palette["disabled_bg"]), ("pressed", palette["button_pressed"]), ("active", palette["button_hover"])], foreground=[("disabled", palette["disabled_text"])])
    style.configure("Accent.TButton", background=palette["accent"], foreground="#FFFFFF", borderwidth=0, padding=(14, 9), relief="flat", font=(sans, 12, "bold"))
    style.map("Accent.TButton", background=[("disabled", palette["disabled_bg"]), ("pressed", palette["accent_pressed"]), ("active", palette["accent_hover"])], foreground=[("disabled", "#FFFFFF")])
    style.configure("TEntry", fieldbackground=palette["field_bg"], background=palette["field_bg"], foreground=palette["text"], insertcolor=palette["text"], borderwidth=1, padding=8, relief="solid")
    style.map("TEntry", fieldbackground=[("disabled", palette["disabled_bg"])], foreground=[("disabled", palette["disabled_text"])])
    style.configure("TCombobox", fieldbackground=palette["field_bg"], background=palette["field_bg"], foreground=palette["text"], arrowcolor=palette["text"], padding=8, relief="solid")
    style.map("TCombobox", fieldbackground=[("readonly", palette["field_bg"]), ("disabled", palette["disabled_bg"])], foreground=[("disabled", palette["disabled_text"])])
    style.configure("Status.TLabel", background=palette["card_bg"], foreground=palette["text"], font=(sans, 12, "bold"))
    style.configure("Detail.TLabel", background=palette["card_bg"], foreground=palette["muted_text"], font=fonts["small"])
    style.configure("Horizontal.TProgressbar", background=palette["accent"], troughcolor="#E8E8ED", borderwidth=0, lightcolor=palette["accent"], darkcolor=palette["accent"])

    return {"palette": palette, "fonts": fonts, "style": style}


# ----------------------------- Utility helpers ----------------------------- #

HMS_RE = re.compile(r"^(\d+):(\d{2})(?::(\d{2}))?$")
TOKEN_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", re.IGNORECASE)
ASS_TIME_RE = re.compile(r"^\s*(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})(?:\s+.*)?$")
ARABIC_CHAR_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")


def read_text_any_encoding(path: str | Path) -> str:
    path = str(path)
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=encoding) as handle:
                return handle.read()
        except UnicodeDecodeError:
            continue
    with open(path, "rb") as handle:
        data = handle.read()
    return data.decode("latin-1", errors="replace")


@dataclass
class Cue:
    start_ms: int
    end_ms: int
    text_lines: list[str]


@dataclass
class SegmentOutput:
    video_path: Path
    subtitle_paths: list[Path]
    ass_paths: list[Path]


@dataclass
class SrtBlock:
    start_ms: int
    end_ms: int
    lines: list[str]


def parse_duration_seconds(value: str) -> int:
    raw = value.strip().lower()
    if not raw:
        raise ValueError("Duration is empty.")
    match = HMS_RE.match(raw)
    if match:
        a, b, c = match.groups()
        if c is None:
            return int(a) * 60 + int(b)
        return int(a) * 3600 + int(b) * 60 + int(c)
    if raw.isdigit():
        return int(raw)
    match = TOKEN_RE.match(raw)
    if match and any(group for group in match.groups()):
        h = int(match.group(1) or 0)
        m = int(match.group(2) or 0)
        s = int(match.group(3) or 0)
        return h * 3600 + m * 60 + s
    raise ValueError(f"Could not parse duration: {value!r}")


def parse_cut_point_seconds(value: str) -> float:
    raw = value.strip().lower()
    if not raw:
        raise ValueError("Cut point is empty.")
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return float(raw) * 60.0
    return float(parse_duration_seconds(raw))


def format_seconds_compact(seconds: float) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def list_videos(directory: str | Path) -> list[str]:
    base = Path(directory).expanduser()
    if not base.is_dir():
        return []
    files = [p.name for p in base.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS]
    return sorted(files, key=str.lower)


def matching_srt_files(directory: str | Path, video_filename: str) -> list[str]:
    base = Path(directory).expanduser()
    if not base.is_dir() or not video_filename:
        return []
    stem = Path(video_filename).stem
    exact = []
    prefixed = []
    others = []
    for path in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file() or path.suffix.lower() != ".srt":
            continue
        name = path.name
        if name == f"{stem}.srt":
            exact.append(name)
        elif name.startswith(f"{stem}."):
            prefixed.append(name)
        else:
            others.append(name)
    return exact + prefixed + others


def ffprobe_duration_seconds(path: str | Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "ffprobe failed")
    text = result.stdout.strip()
    if not text or text == "N/A":
        raise RuntimeError("Could not read media duration.")
    return float(text)


def ensure_ffmpeg(logger: Callable[[str], None]) -> None:
    for name in ("ffprobe", "ffmpeg"):
        result = subprocess.run(["/usr/bin/env", "bash", "-lc", f"command -v {name}"], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"{name} was not found on PATH. Install ffmpeg first, e.g. brew install ffmpeg")
    logger("> ffmpeg / ffprobe detected.")


def run_cmd(cmd: list[str], logger: Callable[[str], None]) -> None:
    logger("> " + " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.stdout.strip():
        logger(proc.stdout.strip())
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or f"Command failed with exit code {proc.returncode}"
        raise RuntimeError(err)


def parse_srt_file(path: str | Path) -> list[Cue]:
    content = read_text_any_encoding(path).replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    blocks = re.split(r"\n\s*\n", content.strip())
    cues: list[Cue] = []
    for block in blocks:
        lines = block.splitlines()
        if not lines:
            continue
        time_line_index = None
        match = None
        for idx, line in enumerate(lines):
            match = ASS_TIME_RE.match(line.strip())
            if match:
                time_line_index = idx
                break
        if time_line_index is None or match is None:
            continue
        start_ms = srt_ts_to_ms(match.group(1))
        end_ms = srt_ts_to_ms(match.group(2))
        text_lines = lines[time_line_index + 1 :]
        cues.append(Cue(start_ms=start_ms, end_ms=end_ms, text_lines=text_lines))
    return cues


def srt_ts_to_ms(ts: str) -> int:
    hh, mm, rest = ts.split(":")
    ss, ms = rest.split(",")
    return (int(hh) * 3600 + int(mm) * 60 + int(ss)) * 1000 + int(ms)


def ms_to_srt_ts(ms: int) -> str:
    if ms < 0:
        ms = 0
    h = ms // 3_600_000
    ms %= 3_600_000
    m = ms // 60_000
    ms %= 60_000
    s = ms // 1000
    rem = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{rem:03d}"


def write_srt_file(cues: list[Cue], path: str | Path) -> None:
    lines: list[str] = []
    for index, cue in enumerate(cues, start=1):
        lines.append(str(index))
        lines.append(f"{ms_to_srt_ts(cue.start_ms)} --> {ms_to_srt_ts(cue.end_ms)}")
        lines.extend(cue.text_lines if cue.text_lines else [""])
        lines.append("")
    output = "\n".join(lines).rstrip() + "\n"
    with open(path, "w", encoding="utf-8-sig", newline="\n") as handle:
        handle.write(output)


def split_srt_range(cues: list[Cue], segment_start_ms: int, segment_end_ms: int) -> list[Cue]:
    out: list[Cue] = []
    for cue in cues:
        if cue.end_ms <= segment_start_ms or cue.start_ms >= segment_end_ms:
            continue
        new_start = max(cue.start_ms, segment_start_ms) - segment_start_ms
        new_end = min(cue.end_ms, segment_end_ms) - segment_start_ms
        if new_end <= new_start:
            continue
        out.append(Cue(start_ms=new_start, end_ms=new_end, text_lines=list(cue.text_lines)))
    return out


def split_srt_window_file(input_path: str | Path, start_seconds: float, length_seconds: float, output_path: str | Path) -> None:
    cues = parse_srt_file(input_path)
    segment = split_srt_range(cues, int(round(start_seconds * 1000)), int(round((start_seconds + length_seconds) * 1000)))
    write_srt_file(segment, output_path)


def split_srt_cut_file(input_path: str | Path, cut_seconds: float, output_path_1: str | Path, output_path_2: str | Path) -> None:
    cues = parse_srt_file(input_path)
    cut_ms = int(round(cut_seconds * 1000))
    part1 = split_srt_range(cues, 0, cut_ms)
    max_end = max((cue.end_ms for cue in cues), default=cut_ms)
    part2 = split_srt_range(cues, cut_ms, max(max_end, cut_ms + 1))
    write_srt_file(part1, output_path_1)
    write_srt_file(part2, output_path_2)


# ----------------------------- ASS helpers ----------------------------- #


def parse_srt_blocks_from_file(path: str | Path) -> list[SrtBlock]:
    return [SrtBlock(start_ms=cue.start_ms, end_ms=cue.end_ms, lines=cue.text_lines) for cue in parse_srt_file(path)]


def _normalize_lines(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if line.strip()]


def _flatten_text(lines: list[str]) -> str:
    return " ".join(_normalize_lines(lines)).strip()


def _dedupe_keep_order(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item and item not in seen:
            out.append(item)
            seen.add(item)
    return out


def _center_ms(block: SrtBlock) -> int:
    return (block.start_ms + block.end_ms) // 2


def _ass_escape_text(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def _ass_escape_lines(lines: list[str]) -> str:
    return r"\N".join(_ass_escape_text(line) for line in _normalize_lines(lines))


def ms_to_ass_ts(ms: int) -> str:
    if ms < 0:
        ms = 0
    cs = (ms + 5) // 10
    hh = cs // 360000
    cs %= 360000
    mm = cs // 6000
    cs %= 6000
    ss = cs // 100
    cc = cs % 100
    return f"{hh:d}:{mm:02d}:{ss:02d}.{cc:02d}"


def collect_source_text_for_block(
    ar_block: SrtBlock,
    source_blocks: list[SrtBlock],
    start_index: int,
    min_overlap_ms: int,
    nearest_ms: int,
    joiner: str,
) -> tuple[str, int]:
    i = start_index
    n = len(source_blocks)
    while i < n and source_blocks[i].end_ms <= ar_block.start_ms:
        i += 1
    j = i
    matches: list[str] = []
    while j < n and source_blocks[j].start_ms < ar_block.end_ms:
        overlap = min(ar_block.end_ms, source_blocks[j].end_ms) - max(ar_block.start_ms, source_blocks[j].start_ms)
        if overlap >= min_overlap_ms:
            text = _flatten_text(source_blocks[j].lines)
            if text:
                matches.append(text)
        j += 1
    matches = _dedupe_keep_order(matches)
    if matches:
        return joiner.join(matches), i
    if nearest_ms <= 0:
        return "", i
    candidates: list[tuple[int, SrtBlock]] = []
    target_center = _center_ms(ar_block)
    if i - 1 >= 0:
        prev_block = source_blocks[i - 1]
        candidates.append((abs(_center_ms(prev_block) - target_center), prev_block))
    if i < n:
        next_block = source_blocks[i]
        candidates.append((abs(_center_ms(next_block) - target_center), next_block))
    if not candidates:
        return "", i
    candidates.sort(key=lambda item: item[0])
    distance, best = candidates[0]
    if distance <= nearest_ms:
        return _flatten_text(best.lines), i
    return "", i


def build_combined_text(ar_lines: list[str], source_text: str, original_font_size: int, original_bgr: str, original_outline: int) -> str:
    ar_text = _ass_escape_lines(ar_lines)
    if not source_text:
        return ar_text
    src = _ass_escape_text(source_text)
    original_tag = (
        r"{" + f"\\fs{original_font_size}" + f"\\bord{original_outline}" + r"\c&H" + original_bgr + r"&" + r"\3c&H000000&" + r"}"
    )
    reset_tag = r"{\r}"
    return f"{ar_text}\\N{original_tag}{src}{reset_tag}"


def render_ass_single(blocks: list[SrtBlock], model_text: str) -> str:
    purple = "&H00FF00B4"
    yellow = "&H0000D5FF"
    black = "&H00000000"
    font = "Arial"
    font_size = 84
    outline = 6
    shadow = 0
    margin_bottom = 55
    model_margin_top = 55

    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "Collisions: Normal",
        "ScaledBorderAndShadow: yes",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "WrapStyle: 0",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font},{font_size},{purple},{purple},{black},{black},1,0,0,0,100,100,0,0,1,{outline},{shadow},2,70,70,{margin_bottom},1",
        f"Style: ModelTop,{font},{font_size},{yellow},{yellow},{black},{black},1,0,0,0,100,100,0,0,1,{outline},{shadow},8,70,70,{model_margin_top},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    events: list[str] = []
    forced_text_tag = (
        r"{\an2" + f"\\fs{font_size}" + f"\\bord{outline}" + f"\\shad{shadow}" + r"\c&HFF00B4&" + r"\3c&H000000&" + r"}"
    )
    forced_model_tag = (
        r"{\an8" + f"\\fs{font_size}" + f"\\bord{outline}" + f"\\shad{shadow}" + r"\c&H00D5FF&" + r"\3c&H000000&" + r"}"
    )
    max_end_ms = 0
    for block in blocks:
        text = _ass_escape_lines(block.lines)
        if not text:
            continue
        events.append(f"Dialogue: 0,{ms_to_ass_ts(block.start_ms)},{ms_to_ass_ts(block.end_ms)},Default,,0,0,0,,{forced_text_tag}{text}")
        max_end_ms = max(max_end_ms, block.end_ms)
    if model_text.strip():
        if max_end_ms <= 0:
            max_end_ms = 1
        start_ms = max(0, max_end_ms - 60000)
        events.append(f"Dialogue: 10,{ms_to_ass_ts(start_ms)},{ms_to_ass_ts(max_end_ms)},ModelTop,,0,0,0,,{forced_model_tag}{_ass_escape_text(model_text.strip())}")
    return "\n".join(header + events) + "\n"


def render_ass_bilingual(ar_blocks: list[SrtBlock], source_blocks: list[SrtBlock], model_text: str) -> str:
    purple = "&H00FF00B4"
    yellow = "&H0000D5FF"
    black = "&H00000000"
    yellow_bgr = "00D5FF"
    font = "Arial"
    font_size = 84
    original_font_size = 63
    outline = 6
    shadow = 0
    margin_bottom = 90
    model_margin_top = 55

    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "Collisions: Normal",
        "ScaledBorderAndShadow: yes",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "WrapStyle: 0",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font},{font_size},{purple},{purple},{black},{black},1,0,0,0,100,100,0,0,1,{outline},{shadow},2,70,70,{margin_bottom},1",
        f"Style: ModelTop,{font},{font_size},{yellow},{yellow},{black},{black},1,0,0,0,100,100,0,0,1,{outline},{shadow},8,70,70,{model_margin_top},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    forced_ar_tag = (
        r"{\an2" + f"\\fs{font_size}" + f"\\bord{outline}" + f"\\shad{shadow}" + r"\c&HFF00B4&" + r"\3c&H000000&" + r"}"
    )
    forced_model_tag = (
        r"{\an8" + f"\\fs{font_size}" + f"\\bord{outline}" + f"\\shad{shadow}" + r"\c&H00D5FF&" + r"\3c&H000000&" + r"}"
    )

    events: list[str] = []
    source_index = 0
    max_end_ms = 0
    for ar_block in ar_blocks:
        source_text, source_index = collect_source_text_for_block(
            ar_block=ar_block,
            source_blocks=source_blocks,
            start_index=source_index,
            min_overlap_ms=1,
            nearest_ms=400,
            joiner=" / ",
        )
        text = build_combined_text(
            ar_lines=ar_block.lines,
            source_text=source_text,
            original_font_size=original_font_size,
            original_bgr=yellow_bgr,
            original_outline=max(2, max(1, outline // 2)),
        )
        if not text:
            continue
        events.append(f"Dialogue: 0,{ms_to_ass_ts(ar_block.start_ms)},{ms_to_ass_ts(ar_block.end_ms)},Default,,0,0,0,,{forced_ar_tag}{text}")
        max_end_ms = max(max_end_ms, ar_block.end_ms)
    if source_blocks:
        max_end_ms = max(max_end_ms, max(block.end_ms for block in source_blocks))
    if model_text.strip():
        if max_end_ms <= 0:
            max_end_ms = 1
        start_ms = max(0, max_end_ms - 60000)
        events.append(f"Dialogue: 10,{ms_to_ass_ts(start_ms)},{ms_to_ass_ts(max_end_ms)},ModelTop,,0,0,0,,{forced_model_tag}{_ass_escape_text(model_text.strip())}")
    return "\n".join(header + events) + "\n"


def create_single_ass_from_srt(srt_path: str | Path, output_ass_path: str | Path, model_text: str) -> None:
    blocks = parse_srt_blocks_from_file(srt_path)
    if not blocks:
        raise RuntimeError(f"No subtitle blocks parsed from: {srt_path}")
    text = render_ass_single(blocks, model_text)
    with open(output_ass_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def create_bilingual_ass(source_srt_path: str | Path, arabic_srt_path: str | Path, output_ass_path: str | Path, model_text: str) -> None:
    source_blocks = parse_srt_blocks_from_file(source_srt_path)
    arabic_blocks = parse_srt_blocks_from_file(arabic_srt_path)
    if not source_blocks:
        raise RuntimeError(f"No subtitle blocks parsed from source file: {source_srt_path}")
    if not arabic_blocks:
        raise RuntimeError(f"No subtitle blocks parsed from Arabic file: {arabic_srt_path}")
    text = render_ass_bilingual(arabic_blocks, source_blocks, model_text)
    with open(output_ass_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


# ----------------------------- Split operations ----------------------------- #


def split_video_two_parts(input_video: Path, cut_seconds: float, output_dir: Path, logger: Callable[[str], None]) -> tuple[Path, Path]:
    base = input_video.stem
    ext = input_video.suffix
    out1 = output_dir / f"{base}.part1{ext}"
    out2 = output_dir / f"{base}.part2{ext}"
    logger(f"> Splitting video into two parts at {format_seconds_compact(cut_seconds)}")
    run_cmd([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(input_video),
        "-to", f"{cut_seconds:.3f}",
        "-map", "0", "-c", "copy",
        str(out1),
    ], logger)
    run_cmd([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{cut_seconds:.3f}",
        "-i", str(input_video),
        "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero",
        str(out2),
    ], logger)
    return out1, out2


def split_video_windows(input_video: Path, duration_seconds: int, overlap_seconds: int, preseek_seconds: int, output_dir: Path, accurate_copy: bool, logger: Callable[[str], None]) -> list[tuple[Path, float, float]]:
    total = math.floor(ffprobe_duration_seconds(input_video))
    if duration_seconds <= overlap_seconds:
        raise RuntimeError(f"Part duration must be greater than overlap ({overlap_seconds}s).")
    step = duration_seconds - overlap_seconds
    base = input_video.stem
    ext = input_video.suffix
    outputs: list[tuple[Path, float, float]] = []
    start = 0
    index = 1
    while start < total:
        length = duration_seconds
        if start + length > total:
            length = total - start
        out_path = output_dir / f"{base}_part{index:03d}{ext}"
        logger(f"> part {index}: start={start}s len={length}s -> {out_path.name}")
        if accurate_copy:
            pre = max(0, start - preseek_seconds)
            off = start - pre
            run_cmd([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", str(pre), "-i", str(input_video), "-ss", str(off), "-t", str(length),
                "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero",
                "-map_metadata", "-1", "-map_chapters", "-1",
                str(out_path),
            ], logger)
        else:
            run_cmd([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", str(start), "-i", str(input_video), "-t", str(length),
                "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero",
                str(out_path),
            ], logger)
        outputs.append((out_path, float(start), float(length)))
        start += step
        index += 1
    return outputs


# ----------------------------- GUI ----------------------------- #


class VideoSplitGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Video Splitter GUI (macOS)")
        self.root.geometry("1220x860")
        self.root.minsize(1080, 760)

        theme = apply_theme(root)
        self.palette = theme["palette"]
        self.fonts = theme["fonts"]

        self.queue: queue.Queue[tuple] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.running = False

        state = load_gui_state()
        initial_dir = Path(state.get("directory", str(Path.home()))).expanduser()
        if not initial_dir.is_dir():
            initial_dir = Path.home()
        initial_output = Path(state.get("output_dir", str(initial_dir / "split_output"))).expanduser()

        self.directory_var = tk.StringVar(value=str(initial_dir))
        self.output_dir_var = tk.StringVar(value=str(initial_output))
        self.mode_var = tk.StringVar(value=state.get("mode", "cut"))
        self.cut_value_var = tk.StringVar(value=state.get("cut_value", "27"))
        self.window_duration_var = tk.StringVar(value=state.get("window_duration", "10m"))
        self.overlap_var = tk.StringVar(value=state.get("overlap", "60"))
        self.preseek_var = tk.StringVar(value=state.get("preseek", "30"))
        self.split_subs_var = tk.BooleanVar(value=state.get("split_subs", True))
        self.convert_ass_var = tk.BooleanVar(value=state.get("convert_ass", False))
        self.subtitle_a_var = tk.StringVar(value=state.get("subtitle_a", ""))
        self.subtitle_b_var = tk.StringVar(value=state.get("subtitle_b", ""))
        self.model_text_var = tk.StringVar(value=state.get("model_text", "GPT-5.4 Thinking"))
        self.status_var = tk.StringVar(value="Ready.")
        self.progress_text_var = tk.StringVar(value="Idle")

        self.video_paths: list[str] = []
        self.subtitle_choices: list[str] = []

        self.build_ui()
        self.refresh_directory(initial_load=True)
        self.apply_state_rules()
        self.root.after(100, self.process_queue)

    def build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=14, style="App.TFrame")
        main.pack(fill="both", expand=True)

        header = ttk.Frame(main, padding=(18, 14), style="Header.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text="Video Splitter", style="HeaderTitle.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Directory picker + video list + optional SRT split + optional ASS conversion, built for your Mac workflow.",
            style="HeaderSubtitle.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        body = ttk.Frame(main, padding=(0, 12, 0, 0), style="App.TFrame")
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(2, weight=1)

        folder_card = ttk.LabelFrame(body, text="Folder and output", padding=14, style="Card.TLabelframe")
        folder_card.grid(row=0, column=0, columnspan=2, sticky="nsew")
        folder_card.columnconfigure(1, weight=1)

        ttk.Label(folder_card, text="Folder:", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=6)
        ttk.Entry(folder_card, textvariable=self.directory_var).grid(row=0, column=1, sticky="ew", pady=6)
        ttk.Button(folder_card, text="Browse…", command=self.choose_directory).grid(row=0, column=2, padx=(8, 0), pady=6)
        ttk.Button(folder_card, text="Refresh", command=self.refresh_directory).grid(row=0, column=3, padx=(8, 0), pady=6)

        ttk.Label(folder_card, text="Output folder:", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=6)
        ttk.Entry(folder_card, textvariable=self.output_dir_var).grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Button(folder_card, text="Browse…", command=self.choose_output_dir).grid(row=1, column=2, padx=(8, 0), pady=6)
        ttk.Button(folder_card, text="Open output", command=self.open_output_dir).grid(row=1, column=3, padx=(8, 0), pady=6)

        left_card = ttk.LabelFrame(body, text="Pick a video", padding=14, style="Card.TLabelframe")
        left_card.grid(row=1, column=0, rowspan=2, sticky="nsew", padx=(0, 6), pady=(12, 0))
        left_card.rowconfigure(1, weight=1)
        left_card.columnconfigure(0, weight=1)
        ttk.Label(left_card, text="Videos in the selected folder:", style="Card.TLabel").grid(row=0, column=0, sticky="w")

        list_frame = ttk.Frame(left_card, style="Card.TFrame")
        list_frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        self.video_listbox = tk.Listbox(
            list_frame,
            activestyle="dotbox",
            exportselection=False,
            font=self.fonts["base"],
            bg=self.palette["card_bg"],
            fg=self.palette["text"],
            selectbackground="#D7DEF4",
            selectforeground=self.palette["text"],
            highlightthickness=1,
            highlightbackground=self.palette["border"],
            highlightcolor=self.palette["accent"],
            relief="flat",
        )
        self.video_listbox.grid(row=0, column=0, sticky="nsew")
        video_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.video_listbox.yview)
        video_scroll.grid(row=0, column=1, sticky="ns")
        self.video_listbox.configure(yscrollcommand=video_scroll.set)
        self.video_listbox.bind("<<ListboxSelect>>", lambda _event: self.on_video_selected())

        ttk.Label(
            left_card,
            text="When you change the selected video, subtitle choices update automatically.",
            style="Muted.Card.TLabel",
        ).grid(row=2, column=0, sticky="w", pady=(10, 0))

        right_top = ttk.LabelFrame(body, text="Split options", padding=14, style="Card.TLabelframe")
        right_top.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(12, 0))
        right_top.columnconfigure(1, weight=1)
        right_top.columnconfigure(3, weight=1)

        ttk.Label(right_top, text="Mode:", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=6)
        mode_frame = ttk.Frame(right_top, style="Card.TFrame")
        mode_frame.grid(row=0, column=1, columnspan=3, sticky="w", pady=6)
        ttk.Radiobutton(mode_frame, text="Two-part cut", value="cut", variable=self.mode_var, command=self.apply_state_rules, style="Card.TRadiobutton").pack(side="left")
        ttk.Radiobutton(mode_frame, text="Window split with overlap", value="window", variable=self.mode_var, command=self.apply_state_rules, style="Card.TRadiobutton").pack(side="left", padx=(14, 0))

        ttk.Label(right_top, text="Cut point:", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=6)
        self.cut_entry = ttk.Entry(right_top, textvariable=self.cut_value_var)
        self.cut_entry.grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Label(right_top, text="Examples: 27, 27.5, 00:27:00, 27m", style="Muted.Card.TLabel").grid(row=1, column=2, columnspan=2, sticky="w", pady=6)

        ttk.Label(right_top, text="Part duration:", style="Card.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=6)
        self.window_duration_entry = ttk.Entry(right_top, textvariable=self.window_duration_var)
        self.window_duration_entry.grid(row=2, column=1, sticky="ew", pady=6)
        ttk.Label(right_top, text="Examples: 10m, 1h30m, 00:10:00", style="Muted.Card.TLabel").grid(row=2, column=2, columnspan=2, sticky="w", pady=6)

        ttk.Label(right_top, text="Overlap (sec):", style="Card.TLabel").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=6)
        self.overlap_entry = ttk.Entry(right_top, textvariable=self.overlap_var)
        self.overlap_entry.grid(row=3, column=1, sticky="ew", pady=6)

        ttk.Label(right_top, text="Pre-seek (sec):", style="Card.TLabel").grid(row=3, column=2, sticky="w", padx=(12, 8), pady=6)
        self.preseek_entry = ttk.Entry(right_top, textvariable=self.preseek_var)
        self.preseek_entry.grid(row=3, column=3, sticky="ew", pady=6)

        ttk.Label(
            right_top,
            text="Window mode mirrors your old vsplit/vsplit_srt flow: duration + overlap, with pre-seek for safer copy-based cuts.",
            style="Muted.Card.TLabel",
        ).grid(row=4, column=0, columnspan=4, sticky="w", pady=(10, 0))

        right_bottom = ttk.LabelFrame(body, text="Subtitle split and ASS conversion", padding=14, style="Card.TLabelframe")
        right_bottom.grid(row=2, column=1, sticky="nsew", padx=(6, 0), pady=(12, 0))
        right_bottom.columnconfigure(1, weight=1)
        right_bottom.columnconfigure(3, weight=1)

        self.split_subs_check = ttk.Checkbutton(right_bottom, text="Split external subtitles too", variable=self.split_subs_var, command=self.apply_state_rules, style="Card.TCheckbutton")
        self.split_subs_check.grid(row=0, column=0, columnspan=4, sticky="w", pady=6)

        ttk.Label(right_bottom, text="Subtitle 1:", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=6)
        self.subtitle_a_combo = ttk.Combobox(right_bottom, textvariable=self.subtitle_a_var, state="readonly")
        self.subtitle_a_combo.grid(row=1, column=1, sticky="ew", pady=6)

        ttk.Label(right_bottom, text="Subtitle 2:", style="Card.TLabel").grid(row=1, column=2, sticky="w", padx=(12, 8), pady=6)
        self.subtitle_b_combo = ttk.Combobox(right_bottom, textvariable=self.subtitle_b_var, state="readonly")
        self.subtitle_b_combo.grid(row=1, column=3, sticky="ew", pady=6)

        self.convert_ass_check = ttk.Checkbutton(right_bottom, text="Create ASS after splitting subtitles", variable=self.convert_ass_var, command=self.apply_state_rules, style="Card.TCheckbutton")
        self.convert_ass_check.grid(row=2, column=0, columnspan=4, sticky="w", pady=6)

        ttk.Label(right_bottom, text="ASS tail label:", style="Card.TLabel").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=6)
        self.model_entry = ttk.Entry(right_bottom, textvariable=self.model_text_var)
        self.model_entry.grid(row=3, column=1, columnspan=3, sticky="ew", pady=6)

        ttk.Label(
            right_bottom,
            text="If ASS is enabled with 2 subtitles, Subtitle 1 is treated as source and Subtitle 2 as Arabic for bilingual ASS. If only Subtitle 1 is chosen, a single-language ASS is created.",
            style="Muted.Card.TLabel",
        ).grid(row=4, column=0, columnspan=4, sticky="w", pady=(10, 0))

        actions = ttk.Frame(main, style="App.TFrame")
        actions.pack(fill="x", pady=(12, 0))
        self.start_button = ttk.Button(actions, text="Start split", command=self.start_split)
        self.start_button.pack(side="left")
        ttk.Button(actions, text="Clear log", command=self.clear_log).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Quit", command=self.root.destroy).pack(side="right")

        progress_frame = ttk.LabelFrame(main, text="Progress", padding=14, style="Card.TLabelframe")
        progress_frame.pack(fill="x", pady=(12, 0))
        ttk.Label(progress_frame, textvariable=self.status_var, style="Status.TLabel").pack(anchor="w")
        self.progress = ttk.Progressbar(progress_frame, orient="horizontal", mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(8, 4))
        ttk.Label(progress_frame, textvariable=self.progress_text_var, style="Detail.TLabel").pack(anchor="w")

        log_frame = ttk.LabelFrame(main, text="Log", padding=14, style="Card.TLabelframe")
        log_frame.pack(fill="both", expand=True, pady=(12, 0))
        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            wrap="word",
            height=18,
            font=self.fonts["mono"],
            bg=self.palette["card_bg"],
            fg=self.palette["text"],
            insertbackground=self.palette["text"],
            selectbackground="#D7DEF4",
            selectforeground=self.palette["text"],
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=self.palette["border"],
            highlightcolor=self.palette["accent"],
            padx=10,
            pady=10,
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

    def append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def queue_log(self, message: str) -> None:
        self.queue.put(("log", message))

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

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
                    self.running = True
                    self.start_button.configure(state="disabled")
                    self.status_var.set("Split in progress...")
                elif kind == "done":
                    success, summary = item[1], item[2]
                    self.running = False
                    self.start_button.configure(state="normal")
                    self.progress.stop()
                    if self.progress["mode"] != "determinate":
                        self.progress.configure(mode="determinate")
                    self.progress["value"] = 100 if success else 0
                    self.status_var.set(summary)
                    self.progress_text_var.set(summary)
                elif kind == "errorbox":
                    messagebox.showerror(item[1], item[2])
                elif kind == "infobox":
                    messagebox.showinfo(item[1], item[2])
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self.process_queue)

    def apply_state_rules(self) -> None:
        cut_enabled = self.mode_var.get() == "cut"
        window_enabled = self.mode_var.get() == "window"
        split_subs = self.split_subs_var.get()
        convert_ass = self.convert_ass_var.get() and split_subs

        self.cut_entry.configure(state="normal" if cut_enabled else "disabled")
        self.window_duration_entry.configure(state="normal" if window_enabled else "disabled")
        self.overlap_entry.configure(state="normal" if window_enabled else "disabled")
        self.preseek_entry.configure(state="normal" if window_enabled else "disabled")

        combo_state = "readonly" if split_subs else "disabled"
        self.subtitle_a_combo.configure(state=combo_state)
        self.subtitle_b_combo.configure(state=combo_state)
        self.convert_ass_check.configure(state="normal" if split_subs else "disabled")
        self.model_entry.configure(state="normal" if convert_ass else "disabled")
        if not split_subs:
            self.convert_ass_var.set(False)

    def save_state(self) -> None:
        save_gui_state({
            "directory": self.directory_var.get().strip(),
            "output_dir": self.output_dir_var.get().strip(),
            "mode": self.mode_var.get(),
            "cut_value": self.cut_value_var.get().strip(),
            "window_duration": self.window_duration_var.get().strip(),
            "overlap": self.overlap_var.get().strip(),
            "preseek": self.preseek_var.get().strip(),
            "split_subs": self.split_subs_var.get(),
            "convert_ass": self.convert_ass_var.get(),
            "subtitle_a": self.subtitle_a_var.get().strip(),
            "subtitle_b": self.subtitle_b_var.get().strip(),
            "model_text": self.model_text_var.get(),
        })

    def choose_directory(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.directory_var.get() or str(Path.home()))
        if folder:
            self.directory_var.set(folder)
            if not self.output_dir_var.get().strip():
                self.output_dir_var.set(str(Path(folder) / "split_output"))
            self.refresh_directory()

    def choose_output_dir(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.output_dir_var.get() or self.directory_var.get() or str(Path.home()))
        if folder:
            self.output_dir_var.set(folder)

    def open_output_dir(self) -> None:
        path = Path(self.output_dir_var.get().strip()).expanduser()
        if not path.exists():
            messagebox.showwarning("Folder not found", f"This folder does not exist:\n{path}")
            return
        subprocess.Popen(["open", str(path)])

    def refresh_directory(self, initial_load: bool = False) -> None:
        directory = Path(self.directory_var.get().strip()).expanduser()
        if not directory.is_dir():
            if not initial_load:
                messagebox.showerror("Invalid folder", f"This folder does not exist:\n{directory}")
            return
        if initial_load and not self.output_dir_var.get().strip():
            self.output_dir_var.set(str(directory / "split_output"))
        elif not initial_load and Path(self.output_dir_var.get().strip()).expanduser() == Path(self.directory_var.get().strip()).expanduser() / "split_output":
            self.output_dir_var.set(str(directory / "split_output"))
        videos = list_videos(directory)
        self.video_paths = videos
        self.video_listbox.delete(0, "end")
        for name in videos:
            self.video_listbox.insert("end", name)
        if videos:
            self.video_listbox.selection_set(0)
            self.video_listbox.activate(0)
            self.on_video_selected()
            self.status_var.set(f"Loaded {len(videos)} video(s) from {directory}")
        else:
            self.subtitle_choices = []
            self.subtitle_a_combo["values"] = [""]
            self.subtitle_b_combo["values"] = [""]
            self.subtitle_a_var.set("")
            self.subtitle_b_var.set("")
            self.status_var.set("No videos found in the selected folder.")
        self.save_state()

    def selected_video_name(self) -> str:
        selection = self.video_listbox.curselection()
        if not selection:
            return ""
        index = int(selection[0])
        if index < 0 or index >= len(self.video_paths):
            return ""
        return self.video_paths[index]

    def on_video_selected(self) -> None:
        directory = Path(self.directory_var.get().strip()).expanduser()
        video_name = self.selected_video_name()
        subtitles = matching_srt_files(directory, video_name)
        self.subtitle_choices = subtitles
        values = [""] + subtitles
        self.subtitle_a_combo["values"] = values
        self.subtitle_b_combo["values"] = values
        if self.subtitle_a_var.get() not in values:
            self.subtitle_a_var.set(subtitles[0] if subtitles else "")
        if self.subtitle_b_var.get() not in values:
            self.subtitle_b_var.set(subtitles[1] if len(subtitles) > 1 else "")
        self.save_state()

    def queue_progress(self, percent: float | None, text: str, indeterminate: bool = False) -> None:
        self.queue.put(("progress", percent, text, indeterminate))

    def start_split(self) -> None:
        if self.running:
            return

        directory = Path(self.directory_var.get().strip()).expanduser()
        output_dir = Path(self.output_dir_var.get().strip()).expanduser()
        video_name = self.selected_video_name()

        if not directory.is_dir():
            messagebox.showerror("Invalid folder", f"This folder does not exist:\n{directory}")
            return
        if not video_name:
            messagebox.showwarning("No video selected", "Choose a video from the list first.")
            return
        if not output_dir:
            messagebox.showwarning("Missing output folder", "Choose an output folder first.")
            return
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror("Output folder error", str(exc))
            return

        subtitle_a = self.subtitle_a_var.get().strip()
        subtitle_b = self.subtitle_b_var.get().strip()
        if self.split_subs_var.get() and subtitle_a and subtitle_b and subtitle_a == subtitle_b:
            messagebox.showerror("Subtitle selection", "Subtitle 1 and Subtitle 2 cannot be the same file.")
            return
        if self.convert_ass_var.get() and self.split_subs_var.get() and not subtitle_a:
            messagebox.showerror("ASS conversion", "Choose Subtitle 1 before enabling ASS conversion.")
            return
        if self.convert_ass_var.get() and not self.split_subs_var.get():
            messagebox.showerror("ASS conversion", "ASS conversion needs subtitle splitting enabled.")
            return

        config = {
            "directory": str(directory),
            "output_dir": str(output_dir),
            "video_name": video_name,
            "mode": self.mode_var.get(),
            "cut_value": self.cut_value_var.get().strip(),
            "window_duration": self.window_duration_var.get().strip(),
            "overlap": self.overlap_var.get().strip(),
            "preseek": self.preseek_var.get().strip(),
            "split_subs": self.split_subs_var.get(),
            "convert_ass": self.convert_ass_var.get(),
            "subtitle_a": subtitle_a,
            "subtitle_b": subtitle_b,
            "model_text": self.model_text_var.get().strip(),
        }
        self.save_state()
        self.clear_log()
        self.queue.put(("start",))
        self.queue_progress(None, "Preparing split...", indeterminate=True)
        self.worker = threading.Thread(target=self.split_worker, args=(config,), daemon=True)
        self.worker.start()

    def split_worker(self, config: dict) -> None:
        logger = self.queue_log
        try:
            logger("=" * 70)
            logger(" Video Splitter GUI (macOS) ")
            logger("=" * 70)
            logger(f"Folder: {config['directory']}")
            logger(f"Video:  {config['video_name']}")
            logger(f"Output: {config['output_dir']}")
            logger("")
            ensure_ffmpeg(logger)

            directory = Path(config["directory"])
            output_dir = Path(config["output_dir"])
            input_video = directory / config["video_name"]
            if not input_video.is_file():
                raise RuntimeError(f"Selected video not found: {input_video}")

            subtitle_paths: list[Path] = []
            if config["split_subs"]:
                if config["subtitle_a"]:
                    subtitle_paths.append(directory / config["subtitle_a"])
                if config["subtitle_b"]:
                    subtitle_paths.append(directory / config["subtitle_b"])
                for sub in subtitle_paths:
                    if not sub.is_file():
                        raise RuntimeError(f"Subtitle file not found: {sub}")

            ass_outputs: list[Path] = []
            model_text = config["model_text"]
            created_segments: list[SegmentOutput] = []

            if config["mode"] == "cut":
                cut_seconds = parse_cut_point_seconds(config["cut_value"])
                total_duration = ffprobe_duration_seconds(input_video)
                if not (0 < cut_seconds < total_duration):
                    raise RuntimeError("Cut point must be greater than 0 and less than the video duration.")
                self.queue_progress(None, "Splitting video into two parts...", indeterminate=True)
                video_part1, video_part2 = split_video_two_parts(input_video, cut_seconds, output_dir, logger)
                segment1 = SegmentOutput(video_path=video_part1, subtitle_paths=[], ass_paths=[])
                segment2 = SegmentOutput(video_path=video_part2, subtitle_paths=[], ass_paths=[])
                created_segments.extend([segment1, segment2])

                if subtitle_paths:
                    logger("")
                    logger("> Splitting subtitle files...")
                    for sub in subtitle_paths:
                        out1 = output_dir / f"{sub.stem}.part1{sub.suffix}"
                        out2 = output_dir / f"{sub.stem}.part2{sub.suffix}"
                        split_srt_cut_file(sub, cut_seconds, out1, out2)
                        logger(f"  Done: {sub.name} -> {out1.name} / {out2.name}")
                        segment1.subtitle_paths.append(out1)
                        segment2.subtitle_paths.append(out2)

                if config["convert_ass"] and subtitle_paths:
                    logger("")
                    logger("> Creating ASS files from split subtitles...")
                    if len(subtitle_paths) >= 2 and config["subtitle_b"]:
                        src1 = output_dir / f"{Path(config['subtitle_a']).stem}.part1.srt"
                        src2 = output_dir / f"{Path(config['subtitle_a']).stem}.part2.srt"
                        ar1 = output_dir / f"{Path(config['subtitle_b']).stem}.part1.srt"
                        ar2 = output_dir / f"{Path(config['subtitle_b']).stem}.part2.srt"
                        ass1 = output_dir / f"{input_video.stem}.part1.bilingual.ass"
                        ass2 = output_dir / f"{input_video.stem}.part2.bilingual.ass"
                        create_bilingual_ass(src1, ar1, ass1, model_text)
                        create_bilingual_ass(src2, ar2, ass2, model_text)
                        segment1.ass_paths.append(ass1)
                        segment2.ass_paths.append(ass2)
                        ass_outputs.extend([ass1, ass2])
                        logger(f"  Created: {ass1.name}")
                        logger(f"  Created: {ass2.name}")
                    else:
                        srt1 = output_dir / f"{Path(config['subtitle_a']).stem}.part1.srt"
                        srt2 = output_dir / f"{Path(config['subtitle_a']).stem}.part2.srt"
                        ass1 = output_dir / f"{Path(config['subtitle_a']).stem}.part1.ass"
                        ass2 = output_dir / f"{Path(config['subtitle_a']).stem}.part2.ass"
                        create_single_ass_from_srt(srt1, ass1, model_text)
                        create_single_ass_from_srt(srt2, ass2, model_text)
                        segment1.ass_paths.append(ass1)
                        segment2.ass_paths.append(ass2)
                        ass_outputs.extend([ass1, ass2])
                        logger(f"  Created: {ass1.name}")
                        logger(f"  Created: {ass2.name}")

            else:
                duration_seconds = parse_duration_seconds(config["window_duration"])
                overlap_seconds = int(config["overlap"] or "0")
                preseek_seconds = int(config["preseek"] or "0")
                accurate_copy = bool(subtitle_paths)
                self.queue_progress(None, "Splitting video into windows...", indeterminate=True)
                windows = split_video_windows(input_video, duration_seconds, overlap_seconds, preseek_seconds, output_dir, accurate_copy, logger)
                for video_path, _start, _length in windows:
                    created_segments.append(SegmentOutput(video_path=video_path, subtitle_paths=[], ass_paths=[]))

                if subtitle_paths:
                    logger("")
                    logger("> Splitting subtitle files for each window...")
                    for part_index, (_video_path, start_sec, length_sec) in enumerate(windows, start=1):
                        for sub in subtitle_paths:
                            out_srt = output_dir / f"{sub.stem}_part{part_index:03d}.srt"
                            split_srt_window_file(sub, start_sec, length_sec, out_srt)
                            created_segments[part_index - 1].subtitle_paths.append(out_srt)
                            logger(f"  Created: {out_srt.name}")

                if config["convert_ass"] and subtitle_paths:
                    logger("")
                    logger("> Creating ASS files from split subtitles...")
                    for part_index, segment in enumerate(created_segments, start=1):
                        if len(subtitle_paths) >= 2 and config["subtitle_b"]:
                            src = output_dir / f"{Path(config['subtitle_a']).stem}_part{part_index:03d}.srt"
                            ar = output_dir / f"{Path(config['subtitle_b']).stem}_part{part_index:03d}.srt"
                            out_ass = output_dir / f"{input_video.stem}_part{part_index:03d}.bilingual.ass"
                            create_bilingual_ass(src, ar, out_ass, model_text)
                        else:
                            src = output_dir / f"{Path(config['subtitle_a']).stem}_part{part_index:03d}.srt"
                            out_ass = output_dir / f"{Path(config['subtitle_a']).stem}_part{part_index:03d}.ass"
                            create_single_ass_from_srt(src, out_ass, model_text)
                        segment.ass_paths.append(out_ass)
                        ass_outputs.append(out_ass)
                        logger(f"  Created: {out_ass.name}")

            logger("")
            logger("Done.")
            logger("Created video files:")
            for segment in created_segments:
                logger(f"  {segment.video_path.name}")
            subtitle_count = sum(len(segment.subtitle_paths) for segment in created_segments)
            if subtitle_count:
                logger(f"Created subtitle files: {subtitle_count}")
            if ass_outputs:
                logger(f"Created ASS files: {len(ass_outputs)}")

            summary = f"Finished. Created {len(created_segments)} video file(s)"
            if subtitle_count:
                summary += f", {subtitle_count} subtitle file(s)"
            if ass_outputs:
                summary += f", and {len(ass_outputs)} ASS file(s)"
            summary += "."
            self.queue.put(("done", True, summary))
        except Exception as exc:
            logger(f"ERROR: {exc}")
            self.queue.put(("done", False, "Split finished with errors."))
            self.queue.put(("errorbox", "Split failed", str(exc)))


def main() -> None:
    root = tk.Tk(className="VideoSplitterMac")
    app = VideoSplitGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
