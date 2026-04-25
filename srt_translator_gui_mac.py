#!/usr/bin/env python3
"""
srt_translator_gui_mac.py

macOS-adapted GUI tool for:
- Extracting subtitle text lines from <name>.<lang>.srt (lang = en, fr, es)
- Chunking them into groups of 150 lines
- Adding an embedded ChatGPT translation prompt at the top of each chunk
- Letting the user Copy / Erase / Paste the content per chunk (for ChatGPT)
- Rebuilding a perfectly synced Arabic SRT: <name>.ar.srt
- Creating a bilingual ASS file that shows Arabic + original subtitles together
- Opening the related video in VLC (separate button)
- Allowing the user to change the working folder via a directory chooser

Requirements on macOS:
- Python 3 with Tkinter (`brew install python3 python-tk`)
- VLC installed in /Applications or accessible via `open -a VLC`

Usage:
    python3 srt_translator_gui_mac.py
"""

import os
import re
import subprocess
import shutil
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk, messagebox, filedialog, simpledialog
from typing import List, Optional, Dict, Tuple
from pathlib import Path

# ------------- CONFIG -------------

MAX_LINES_PER_CHUNK = 150  # subtitle text lines per tab/chunk

VIDEO_EXTENSIONS = [".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".mpeg", ".mpg"]

# Supported source language codes (filename pattern: <base>.<lang>.srt)
SUPPORTED_LANG_CODES = ["en", "fr", "es"]

TIMECODE_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}$"
)

LINE_ID_RE = re.compile(r"^L\d{6}$")


# ---------- RTL / BiDi forcing ----------
# SRT itself doesn't have "RTL mode", so we force direction using Unicode BiDi marks.
# RLE ... PDF works broadly across players/renderers (including libass in ffmpeg).
FORCE_RTL_BIDI_MARKS = True

BIDI_RLE = "\u202B"  # Right-to-Left Embedding
BIDI_PDF = "\u202C"  # Pop Directional Formatting
BIDI_RLM = "\u200F"  # Right-to-Left Mark (helps some renderers)
BIDI_CONTROL_CHARS = {
    "\u202A", "\u202B", "\u202C", "\u202D", "\u202E",  # LRE/RLE/PDF/LRO/RLO
    "\u2066", "\u2067", "\u2068", "\u2069",            # LRI/RLI/FSI/PDI
    "\u200E", "\u200F",                                # LRM/RLM
}

ARABIC_CHAR_RE = re.compile(
    r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]"
)

def force_rtl_if_arabic(text: str) -> str:
    """
    If the line contains Arabic-script characters, wrap it with BiDi marks
    to force RTL rendering in most subtitle players.
    Also strips any existing BiDi control chars first to avoid double-wrapping.
    """
    if not FORCE_RTL_BIDI_MARKS:
        return text

    if not ARABIC_CHAR_RE.search(text):
        return text

    # Preserve leading/trailing whitespace while wrapping the core.
    lead_ws = len(text) - len(text.lstrip(" \t"))
    trail_ws = len(text) - len(text.rstrip(" \t"))
    prefix = text[:lead_ws]
    suffix = text[len(text) - trail_ws:] if trail_ws else ""
    core = text[lead_ws: len(text) - trail_ws] if trail_ws else text[lead_ws:]

    # Remove any existing BiDi control chars from the core
    core = "".join(ch for ch in core if ch not in BIDI_CONTROL_CHARS)

    # Wrap: RLM + RLE + core + PDF
    # RLM helps some renderers choose RTL as base direction, RLE/PDF enforces it.
    return f"{prefix}{BIDI_RLM}{BIDI_RLE}{core}{BIDI_PDF}{suffix}"

# Prompt (source-language agnostic) – then flattened to one line
_PROMPT_RAW = """You are a professional subtitle translator. The subtitle-style lines I provide are my own original script/dialogue or text that I have the right to translate. They are not copied from any copyrighted movie, TV show, streaming content, or third-party subtitle file. Treat them as authorized user-provided text for translation.

CRITICAL RULES:

Translate ONLY the lines between the markers: ### START LINES and ### END LINES.
For EVERY input line, output EXACTLY ONE line.
Output MUST contain ONLY translated lines in the exact format: L000001|Arabic translation
Copy EVERYTHING before the first "|" exactly (same letters, same digits). Do NOT change the ID.
Replace ONLY the text after "|" with a Modern Standard Arabic (MSA) translation.
Keep the same number of lines as the input lines between the markers.
Do NOT add, remove, merge, split, reorder, renumber, or skip any lines.
Do NOT add headers, explanations, comments, extra blank lines, or code blocks.

FORMAT EXAMPLE (IDs here are examples only, do not copy them):
Input: EX000001|Hello, how are you?
Output: EX000001|مرحبًا، كيف حالك؟

TRANSLATION QUALITY (professional subtitles):

Modern Standard Arabic only (no dialect).
Natural, fluent, broadcast-quality Arabic suitable for subtitles.
Preserve meaning, tone, sarcasm, humor, tension, and character voice.
Use context ONLY from PREVIOUS lines (1–2 cues back) to resolve pronouns/names.
NEVER use future lines (no look-ahead). Do not pull meaning, objects, verbs, or sentence endings from the next cue.
Preserve cue boundary feel: if the English cue is an incomplete fragment, the Arabic MUST also remain an incomplete fragment.
Do NOT add a full stop or closure that would make the Arabic feel ahead. Prefer continuation punctuation like "…" ONLY when the English is clearly incomplete.
Keep discourse fillers to match timing (e.g., "uh/um/you know/like" → "أمم/يعني/كما تعلم/مثلًا").
Keep each line aligned to its original line; never move content to another line.
If a line is incomplete because the sentence continues, translate it as-is (do not complete it).

ANTI-DRIFT / SEGMENTATION LOCK (MANDATORY):

Do NOT finish a sentence early. If the English splits a sentence across two cues, your Arabic must also be split across the same two cues.
Do NOT merge or redistribute meaning across lines (no sentence smoothing across cues).
Keep punctuation strength aligned: avoid turning commas/fragments into full stops if the English continues in the next cue.
Self-check before answering: each Arabic line must translate ONLY its own English line, not EN+1.

TAGS, BRACKETS, AND FORMATTING:

Keep any HTML tags/formatting and translate only the visible text:
<i>Hello</i> → <i>مرحبًا</i>
<b>WARNING</b> → <b>تحذير</b>
Keep brackets/parentheses and translate the content:
[Music] → [موسيقى]
(sighs) → (يتنهّد)

NUMBERS AND UNITS:

Use Western Arabic numerals 0–9 inside Arabic text (e.g. 3، 25، 2049).
If imperial units appear (feet, miles, pounds, Fahrenheit), convert approximately to metric in Arabic.

Now translate the lines between the markers. Remember: output ONLY the translated L-lines, nothing else."""


PROMPT_TEXT = _PROMPT_RAW.strip()
PROMPT_ONE_LINE = " ".join(_PROMPT_RAW.splitlines())

APP_CLASS_NAME = "SRTTranslator"


def apply_app_icon(root: tk.Tk, preferred_names: list[str]) -> None:
    """Best-effort window icon for GNOME/XWayland/Tk."""
    script_dir = Path(__file__).resolve().parent
    candidates = []
    for name in preferred_names:
        candidates.append(script_dir / name)
    for name in ("app_icon.png", "icon.png"):
        candidates.append(script_dir / name)

    for icon_path in candidates:
        try:
            if icon_path.is_file():
                img = tk.PhotoImage(file=str(icon_path))
                root.iconphoto(True, img)
                root._app_icon_ref = img
                return
        except Exception:
            continue


# Drift-check prompt (QC) – copied via a button in each chunk tab
_DRIFT_CHECK_PROMPT_RAW = """You are a professional subtitle QC (quality control) specialist and movie subtitler.

INPUT (ATTACHED FILES)
You will be given TWO ATTACHED SRT FILES in this chat message:
1) Source subtitles (English): {original_srt}
2) Arabic subtitles to verify/fix: {arabic_srt}

GOAL
Detect and correct ANY cue-level misalignment so the Arabic feels 100% in sync with the spoken words.
This includes:
A) Offset drift: Arabic cue i matches English cue i+1 / i+2 (or vice versa).
B) Boundary-bleed drift (IMPORTANT): even if Arabic cue i mostly matches English cue i, it “finishes”
   a sentence/clause that the English continues in cue i+1, making Arabic feel 1 sentence ahead.
   Even if it happens in only 1–3 isolated cues, it is STILL drift and MUST be corrected.
Do NOT dismiss boundary-bleed as “normal segmentation”.

NON‑NEGOTIABLE RULES (VERY IMPORTANT)
1) Treat both attached files as the single source of truth; do NOT invent or hallucinate lines.
2) Preserve the OUTPUT SRT STRUCTURE 100% EXACTLY based on the ARABIC input file:
   - same number of SRT blocks
   - same index numbers
   - same time ranges (timecodes) character‑for‑character
   - same blank-line placement
   - same number of text lines INSIDE EACH BLOCK (do NOT merge/split blocks)
3) Text policy (MINIMAL CHANGE):
   - Prefer the MINIMAL fix: move/shift existing Arabic text content between blocks.
   - Fix BOTH kinds of drift:
     • offset drift (whole-cue shift)
     • boundary-bleed drift (part of a sentence belongs to the next cue)
   - You may adjust Arabic punctuation/ellipsis ONLY if it helps keep the same “sentence continues” feel
     as the English segmentation (e.g., add "…" to show continuation), but do NOT rewrite freely.
   - If (and only if) shifting/re-segmenting cannot fully remove drift, you may re-translate ONLY the
     drifted region, but keep the exact structure rules above.

WHAT TO DO
1) Parse both files as SRT blocks (index, time range, text lines).
2) Structural integrity check:
   - Same number of blocks? Any missing/extra blocks?
   - Time ranges in the same order? Any abnormal jumps?
3) Semantic alignment check (STRICT):
   For each block i:
   - Decide whether Arabic block i is the translation of English block i.
   - Also test whether Arabic block i better matches English i+1 or i+2 (or i-1 / i-2).
   - Additionally test boundary-bleed:
     Does Arabic block i contain meaning that clearly belongs to English block i+1?
     (Examples: Arabic completes a thought, adds the missing object/verb, or closes with a full stop
      while English i is clearly incomplete and continues in i+1.)
   Mark ANY mismatch or boundary-bleed as a FAIL condition.
4) Find the FIRST problem point (earliest in the file):
   - Arabic block index number and start timestamp
   - Type: OFFSET or BOUNDARY-BLEED (or both)
   - Estimated shift amount (for OFFSET): +1, +2, -1, etc.
5) Evidence (VERY IMPORTANT):
   Show 6–10 consecutive blocks around the first problem point in a compact table:
   idx | EN text | AR text (current) | best-matching EN idx | problem type | why (1 sentence)
6) Verdict:
   - PASS only if there is ZERO drift of BOTH types (no offset drift AND no boundary-bleed drift).
   - Otherwise FAIL.

IF FAIL (drift detected — including isolated boundary-bleed)
Produce a FIXED Arabic SRT:
- Keep every SRT block's index numbers and timecodes EXACTLY the same as in the Arabic file.
- Do NOT rewrite timings.
- Fix by moving Arabic text content to the correct block(s):
  • For OFFSET drift: shift whole Arabic block texts starting from the drift point to match best EN indices.
  • For BOUNDARY-BLEED drift: move ONLY the “extra” clause/phrase that belongs to EN i+1 into Arabic block i+1.
    Keep the same number of lines inside each block (redistribute line breaks if needed, but do not add/remove lines).
- Preserve line breaks and any formatting/tags (e.g., <i>…</i>) as they appear in the Arabic file.
- Keep any existing BiDi control characters (RLM/RLE/PDF) untouched; do not add new ones.

SELF‑VERIFY (MANDATORY)
1) Re-run the SAME drift detection on your FIXED Arabic result.
2) The final result must be PASS with ZERO drift (including boundary-bleed). If not, do ONE more correction pass.

OUTPUT REQUIREMENTS (STRICT)
1) A short summary: PASS/FAIL + first problem point (index, timestamp) + type + shift amount (if any).
2) Evidence table.
3) Always provide a corrected Arabic SRT output (even if it ends up identical):
   - Preferred: provide a downloadable file.
   - Filename rule: insert “.fixed” before the final “.srt”
     Example: “Movie.ar.srt” → “Movie.ar.fixed.srt”.
   - If you can attach files, attach the file.
   - Otherwise, paste the corrected SRT as plain text between EXACT markers (NO Markdown fences, no ```):
     BEGIN_SRT
     ...full file...
     END_SRT
"""
DRIFT_CHECK_PROMPT_TEXT = _DRIFT_CHECK_PROMPT_RAW.strip()
# ------------- CORE HELPERS (encoding, parsing, chunking) -------------


def read_srt_lines(path: str) -> List[str]:
    """
    Read an SRT file with automatic encoding fallback.

    Tries in order:
        utf-8-sig, utf-8, cp1252, latin-1

    Returns a list of lines (with newline characters preserved).
    """
    encodings_to_try = ["utf-8-sig", "utf-8", "cp1252", "latin-1"]
    last_error = None

    for enc in encodings_to_try:
        try:
            with open(path, "r", encoding=enc) as f:
                lines = f.readlines()
            print(f"Opened '{path}' using encoding: {enc}")
            return lines
        except UnicodeDecodeError as e:
            last_error = e

    # Last-resort fallback: binary read with replacement
    print(
        f"WARNING: All standard encodings failed for '{path}'. "
        f"Falling back to binary read with replacement. Last error: {last_error}"
    )
    with open(path, "rb") as f:
        data = f.read()
    text = data.decode("latin-1", errors="replace")
    return text.splitlines(keepends=True)


def extract_text_lines_with_ids(srt_path: str) -> List[str]:
    """
    Parse an SRT file and extract only subtitle text lines.
    Each line gets a unique ID like L000001.

    Returns: ["L000001|text line", "L000002|another line", ...]
    """
    lines = read_srt_lines(srt_path)

    output_lines: List[str] = []
    line_counter = 0
    i = 0
    total_lines = len(lines)

    while i < total_lines:
        stripped = lines[i].strip()
        if TIMECODE_RE.match(stripped):
            i += 1
            while i < total_lines and lines[i].strip() != "":
                text_line = lines[i].rstrip("\n").rstrip("\r")
                line_counter += 1
                line_id = f"L{line_counter:06d}"
                output_lines.append(f"{line_id}|{text_line}")
                i += 1
            if i < total_lines and lines[i].strip() == "":
                i += 1
        else:
            i += 1

    print(f"Extracted {line_counter} subtitle text lines from '{srt_path}'.")
    return output_lines


def split_into_chunks_by_lines(
    lines: List[str], max_lines: int = MAX_LINES_PER_CHUNK
) -> List[List[str]]:
    """Split the list of lines into chunks, each with at most `max_lines` entries."""
    return [lines[i: i + max_lines] for i in range(0, len(lines), max_lines)]


def count_text_lines(srt_path: str) -> int:
    """Count the number of subtitle text lines in an SRT (excluding cue numbers, timecodes, blanks)."""
    lines = read_srt_lines(srt_path)
    total_lines = len(lines)
    i = 0
    count = 0

    while i < total_lines:
        stripped = lines[i].strip()
        if TIMECODE_RE.match(stripped):
            i += 1
            while i < total_lines and lines[i].strip() != "":
                count += 1
                i += 1
            if i < total_lines and lines[i].strip() == "":
                i += 1
        else:
            i += 1

    return count



@dataclass
class SrtBlock:
    start_ms: int
    end_ms: int
    lines: List[str]


ASS_TIME_RE = re.compile(
    r"^\s*(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})(?:\s+.*)?$"
)


def ts_to_ms(ts: str) -> int:
    hh = int(ts[0:2])
    mm = int(ts[3:5])
    ss = int(ts[6:8])
    mmm = int(ts[9:12])
    return (((hh * 60 + mm) * 60) + ss) * 1000 + mmm


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


def parse_srt_blocks(content: str) -> List[SrtBlock]:
    lines = content.splitlines()
    blocks: List[SrtBlock] = []
    i = 0
    n = len(lines)

    while i < n:
        while i < n and lines[i].strip() == "":
            i += 1
        if i >= n:
            break

        i += 1
        if i >= n:
            break

        m = ASS_TIME_RE.match(lines[i])
        if not m:
            while i < n and not ASS_TIME_RE.match(lines[i]):
                i += 1
            if i >= n:
                break
            m = ASS_TIME_RE.match(lines[i])
            if not m:
                break

        start_ms = ts_to_ms(m.group(1))
        end_ms = ts_to_ms(m.group(2))
        i += 1

        text_lines: List[str] = []
        while i < n and lines[i].strip() != "":
            text_lines.append(lines[i])
            i += 1

        blocks.append(SrtBlock(start_ms=start_ms, end_ms=end_ms, lines=text_lines))

    blocks.sort(key=lambda b: (b.start_ms, b.end_ms))
    return blocks


def parse_srt_blocks_from_file(path: str) -> List[SrtBlock]:
    return parse_srt_blocks("".join(read_srt_lines(path)))


def _normalize_lines(lines: List[str]) -> List[str]:
    return [line.strip() for line in lines if line.strip()]


def _flatten_text(lines: List[str]) -> str:
    return " ".join(_normalize_lines(lines)).strip()


def _dedupe_keep_order(items: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items:
        if item and item not in seen:
            out.append(item)
            seen.add(item)
    return out


def _center_ms(block: SrtBlock) -> int:
    return (block.start_ms + block.end_ms) // 2


def collect_source_text_for_block(
    ar_block: SrtBlock,
    source_blocks: List[SrtBlock],
    start_index: int,
    min_overlap_ms: int,
    nearest_ms: int,
    joiner: str,
) -> Tuple[str, int]:
    i = start_index
    n = len(source_blocks)

    while i < n and source_blocks[i].end_ms <= ar_block.start_ms:
        i += 1

    j = i
    matches: List[str] = []
    while j < n and source_blocks[j].start_ms < ar_block.end_ms:
        overlap = min(ar_block.end_ms, source_blocks[j].end_ms) - max(ar_block.start_ms, source_blocks[j].start_ms)
        if overlap >= min_overlap_ms:
            txt = _flatten_text(source_blocks[j].lines)
            if txt:
                matches.append(txt)
        j += 1

    matches = _dedupe_keep_order(matches)
    if matches:
        return joiner.join(matches), i

    if nearest_ms <= 0:
        return "", i

    candidates: List[Tuple[int, SrtBlock]] = []
    target_center = _center_ms(ar_block)

    if i - 1 >= 0:
        prev_block = source_blocks[i - 1]
        candidates.append((abs(_center_ms(prev_block) - target_center), prev_block))

    if i < n:
        next_block = source_blocks[i]
        candidates.append((abs(_center_ms(next_block) - target_center), next_block))

    if not candidates:
        return "", i

    candidates.sort(key=lambda x: x[0])
    distance, best = candidates[0]
    if distance <= nearest_ms:
        return _flatten_text(best.lines), i

    return "", i


def _ass_escape_text(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def _ass_escape_lines(lines: List[str]) -> str:
    clean = _normalize_lines(lines)
    return r"\N".join(_ass_escape_text(line) for line in clean)


def build_combined_text(
    ar_lines: List[str],
    source_text: str,
    original_font_size: int,
    original_bgr: str,
    original_outline: int,
) -> str:
    ar_text = _ass_escape_lines(ar_lines)
    if not source_text:
        return ar_text

    src = _ass_escape_text(source_text)
    original_tag = (
        r"{"
        + f"\\fs{original_font_size}"
        + f"\\bord{original_outline}"
        + r"\c&H" + original_bgr + r"&"
        + r"\3c&H000000&"
        + r"}"
    )
    reset_tag = r"{\r}"
    return f"{ar_text}\\N{original_tag}{src}{reset_tag}"


def render_ass_bilingual(
    ar_blocks: List[SrtBlock],
    source_blocks: List[SrtBlock],
    model_text: str,
    tail_ms: int = 60000,
    playres: str = "1920x1080",
    font: str = "Arial",
    font_size: int = 84,
    original_font_size: int = 63,
    margin_bottom: int = 90,
    model_margin_top: int = 55,
    outline: int = 6,
    shadow: int = 0,
    joiner: str = " / ",
    min_overlap_ms: int = 1,
    nearest_ms: int = 400,
) -> str:
    PURPLE_STYLE = "&H00FF00B4"
    YELLOW_STYLE = "&H0000D5FF"
    BLACK = "&H00000000"
    YELLOW_BGR = "00D5FF"

    if "x" in playres:
        rx, ry = playres.split("x", 1)
        try:
            playresx = int(rx)
            playresy = int(ry)
        except ValueError:
            playresx, playresy = 1920, 1080
    else:
        playresx, playresy = 1920, 1080

    header = []
    header.append("[Script Info]")
    header.append("ScriptType: v4.00+")
    header.append("Collisions: Normal")
    header.append("ScaledBorderAndShadow: yes")
    header.append(f"PlayResX: {playresx}")
    header.append(f"PlayResY: {playresy}")
    header.append("WrapStyle: 0")
    header.append("")
    header.append("[V4+ Styles]")
    header.append(
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding"
    )
    header.append(
        f"Style: Default,{font},{font_size},{PURPLE_STYLE},{PURPLE_STYLE},{BLACK},{BLACK},"
        f"1,0,0,0,100,100,0,0,1,{outline},{shadow},2,70,70,{margin_bottom},1"
    )
    header.append(
        f"Style: ModelTop,{font},{font_size},{YELLOW_STYLE},{YELLOW_STYLE},{BLACK},{BLACK},"
        f"1,0,0,0,100,100,0,0,1,{outline},{shadow},8,70,70,{model_margin_top},1"
    )
    header.append("")
    header.append("[Events]")
    header.append("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text")

    events = []
    source_index = 0

    forced_ar_tag = (
        r"{\an2"
        + f"\\fs{font_size}"
        + f"\\bord{outline}"
        + f"\\shad{shadow}"
        + r"\c&HFF00B4&"
        + r"\3c&H000000&"
        + r"}"
    )
    forced_model_tag = (
        r"{\an8"
        + f"\\fs{font_size}"
        + f"\\bord{outline}"
        + f"\\shad{shadow}"
        + r"\c&H00D5FF&"
        + r"\3c&H000000&"
        + r"}"
    )

    max_end_ms = 0

    for ar_block in ar_blocks:
        source_text, source_index = collect_source_text_for_block(
            ar_block=ar_block,
            source_blocks=source_blocks,
            start_index=source_index,
            min_overlap_ms=min_overlap_ms,
            nearest_ms=nearest_ms,
            joiner=joiner,
        )

        text = build_combined_text(
            ar_lines=ar_block.lines,
            source_text=source_text,
            original_font_size=original_font_size,
            original_bgr=YELLOW_BGR,
            original_outline=max(2, max(1, outline // 2)),
        )

        if not text:
            continue

        s = ms_to_ass_ts(ar_block.start_ms)
        e = ms_to_ass_ts(ar_block.end_ms)
        events.append(f"Dialogue: 0,{s},{e},Default,,0,0,0,,{forced_ar_tag}{text}")
        if ar_block.end_ms > max_end_ms:
            max_end_ms = ar_block.end_ms

    if source_blocks:
        source_end_ms = max(b.end_ms for b in source_blocks)
        if source_end_ms > max_end_ms:
            max_end_ms = source_end_ms

    if model_text.strip():
        if max_end_ms <= 0:
            max_end_ms = max(tail_ms, 1)
        model_start_ms = max(0, max_end_ms - max(0, tail_ms))
        events.append(
            f"Dialogue: 10,{ms_to_ass_ts(model_start_ms)},{ms_to_ass_ts(max_end_ms)},ModelTop,,0,0,0,,"
            f"{forced_model_tag}{_ass_escape_text(model_text.strip())}"
        )

    return "\n".join(header + events) + "\n"


def render_ass_arabic_only(
    ar_blocks: List[SrtBlock],
    model_text: str,
    tail_ms: int = 60000,
    playres: str = "1920x1080",
    font: str = "Arial",
    font_size: int = 84,
    margin_bottom: int = 55,
    model_margin_top: int = 55,
    outline: int = 6,
    shadow: int = 0,
) -> str:
    PURPLE_STYLE = "&H00FF00B4"
    YELLOW_STYLE = "&H0000D5FF"
    BLACK = "&H00000000"

    if "x" in playres:
        rx, ry = playres.split("x", 1)
        try:
            playresx = int(rx)
            playresy = int(ry)
        except ValueError:
            playresx, playresy = 1920, 1080
    else:
        playresx, playresy = 1920, 1080

    header = []
    header.append("[Script Info]")
    header.append("ScriptType: v4.00+")
    header.append("Collisions: Normal")
    header.append("ScaledBorderAndShadow: yes")
    header.append(f"PlayResX: {playresx}")
    header.append(f"PlayResY: {playresy}")
    header.append("WrapStyle: 0")
    header.append("")
    header.append("[V4+ Styles]")
    header.append(
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding"
    )
    header.append(
        f"Style: Default,{font},{font_size},{PURPLE_STYLE},{PURPLE_STYLE},{BLACK},{BLACK},"
        f"1,0,0,0,100,100,0,0,1,{outline},{shadow},2,70,70,{margin_bottom},1"
    )
    header.append(
        f"Style: ModelTop,{font},{font_size},{YELLOW_STYLE},{YELLOW_STYLE},{BLACK},{BLACK},"
        f"1,0,0,0,100,100,0,0,1,{outline},{shadow},8,70,70,{model_margin_top},1"
    )
    header.append("")
    header.append("[Events]")
    header.append("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text")

    events = []
    forced_ar_tag = (
        r"{\an2"
        + f"\\fs{font_size}"
        + f"\\bord{outline}"
        + f"\\shad{shadow}"
        + r"\c&HFF00B4&"
        + r"\3c&H000000&"
        + r"}"
    )
    forced_model_tag = (
        r"{\an8"
        + f"\\fs{font_size}"
        + f"\\bord{outline}"
        + f"\\shad{shadow}"
        + r"\c&H00D5FF&"
        + r"\3c&H000000&"
        + r"}"
    )

    max_end_ms = 0

    for ar_block in ar_blocks:
        text = _ass_escape_lines(ar_block.lines)
        if not text:
            continue

        s = ms_to_ass_ts(ar_block.start_ms)
        e = ms_to_ass_ts(ar_block.end_ms)
        events.append(f"Dialogue: 0,{s},{e},Default,,0,0,0,,{forced_ar_tag}{text}")
        if ar_block.end_ms > max_end_ms:
            max_end_ms = ar_block.end_ms

    if model_text.strip():
        if max_end_ms <= 0:
            max_end_ms = max(tail_ms, 1)
        model_start_ms = max(0, max_end_ms - max(0, tail_ms))
        events.append(
            f"Dialogue: 10,{ms_to_ass_ts(model_start_ms)},{ms_to_ass_ts(max_end_ms)},ModelTop,,0,0,0,,"
            f"{forced_model_tag}{_ass_escape_text(model_text.strip())}"
        )

    return "\n".join(header + events) + "\n"


def create_arabic_only_ass(arabic_srt_path: str, output_ass_path: str, model_text: str) -> None:
    ar_blocks = parse_srt_blocks_from_file(arabic_srt_path)

    if not ar_blocks:
        raise RuntimeError(f"No subtitle blocks parsed from Arabic file: {arabic_srt_path}")

    ass_text = render_ass_arabic_only(
        ar_blocks=ar_blocks,
        model_text=model_text,
        tail_ms=int(10.0 * 6000),
        playres="1920x1080",
        font="Arial",
        font_size=84,
        margin_bottom=55,
        model_margin_top=55,
        outline=6,
        shadow=0,
    )

    with open(output_ass_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(ass_text)

def create_bilingual_ass(source_srt_path: str, arabic_srt_path: str, output_ass_path: str, model_text: str) -> None:
    ar_blocks = parse_srt_blocks_from_file(arabic_srt_path)
    src_blocks = parse_srt_blocks_from_file(source_srt_path)

    if not ar_blocks:
        raise RuntimeError(f"No subtitle blocks parsed from Arabic file: {arabic_srt_path}")
    if not src_blocks:
        raise RuntimeError(f"No subtitle blocks parsed from source file: {source_srt_path}")

    ass_text = render_ass_bilingual(
        ar_blocks=ar_blocks,
        source_blocks=src_blocks,
        model_text=model_text,
        tail_ms=int(10.0 * 6000),
        playres="1920x1080",
        font="Arial",
        font_size=84,
        original_font_size=63,
        margin_bottom=90,
        model_margin_top=55,
        outline=6,
        shadow=0,
        joiner=" / ",
        min_overlap_ms=1,
        nearest_ms=400,
    )

    with open(output_ass_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(ass_text)


def rebuild_srt_sequential(srt_path: str, arabic_lines: List[str], target_path: str) -> None:
    """
    Rebuild the SRT using arabic_lines in order.
    Keeps cue numbers and timecodes exactly as in the original.
    Writes to target_path.

    RTL FIX:
    - If a line contains Arabic characters, we wrap it in Unicode BiDi marks
      so it renders right-to-left in most players.
    - We also write using UTF-8 with BOM (utf-8-sig) for better compatibility.
    """
    lines = read_srt_lines(srt_path)

    output_lines: List[str] = []
    total_lines = len(lines)
    i = 0
    idx_ar = 0
    total_ar = len(arabic_lines)

    while i < total_lines:
        stripped = lines[i].strip()
        if TIMECODE_RE.match(stripped):
            output_lines.append(lines[i])
            i += 1

            while i < total_lines and lines[i].strip() != "":
                if idx_ar >= total_ar:
                    raise RuntimeError("Ran out of Arabic lines before finishing the SRT.")
                arabic_text = arabic_lines[idx_ar]
                idx_ar += 1

                # ---- RTL forcing happens HERE ----
                arabic_text = force_rtl_if_arabic(arabic_text)

                output_lines.append(arabic_text + "\n")
                i += 1

            if i < total_lines and lines[i].strip() == "":
                output_lines.append(lines[i])
                i += 1
        else:
            output_lines.append(lines[i])
            i += 1

    if idx_ar != total_ar:
        print(f"WARNING: Not all translated lines were used. Used {idx_ar}, total {total_ar}.")

    # Write UTF-8 with BOM for maximum subtitle player compatibility
    with open(target_path, "w", encoding="utf-8-sig", newline="\n") as f:
        f.writelines(output_lines)

    print(f"Rebuilt Arabic SRT written to '{target_path}'.")
    print(f"Total subtitle text lines replaced: {idx_ar}")


def find_source_srt_files(base_dir: str) -> List[str]:
    """
    Return list of <base>.<lang>.srt files in base_dir
    where <lang> is one of SUPPORTED_LANG_CODES.
    """
    files: List[str] = []
    try:
        entries = os.listdir(base_dir)
    except FileNotFoundError:
        return []
    for f in entries:
        full_path = os.path.join(base_dir, f)
        if not f.endswith(".srt") or not os.path.isfile(full_path):
            continue
        parts = f.rsplit(".", 2)
        if len(parts) != 3:
            continue
        base, lang_code, ext = parts
        if ext != "srt":
            continue
        if lang_code in SUPPORTED_LANG_CODES:
            files.append(f)
    files.sort()
    return files


def find_video_for_base(base_name: str, base_dir: str) -> Optional[str]:
    """Try to find a video file named <base_name><ext> in base_dir."""
    for ext in VIDEO_EXTENSIONS:
        candidate = os.path.join(base_dir, base_name + ext)
        if os.path.isfile(candidate):
            return candidate
    return None


def open_video_in_vlc(path: str) -> None:
    """Try to open video file in VLC."""
    try:
        subprocess.Popen(["vlc", path])
    except FileNotFoundError:
        messagebox.showwarning(
            "VLC not found",
            "Could not run 'vlc'. Make sure VLC is installed and in your PATH.",
        )
    except Exception as e:
        messagebox.showwarning("Error launching VLC", f"Could not open video in VLC:\n{e}")


# ------------- GUI APP -------------

class SRTTranslatorGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("SRT Translator GUI for macOS (en/fr/es → ar)")
        self.root.geometry("1250x780")

        self.current_dir = os.path.expanduser(os.environ.get("HOME", "~"))

        base_font = ("Arial", 12)
        big_font = ("Arial", 13)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", font=base_font)
        style.configure("TButton", font=big_font, padding=6)
        style.configure("TLabel", font=big_font)
        style.configure("TNotebook.Tab", font=big_font, padding=[10, 6])
        style.configure("TCombobox", font=big_font)

        style.configure("TFrame", background="#222222")
        style.configure("TLabel", background="#222222", foreground="#ffffff")
        style.configure("TNotebook", background="#222222")
        style.configure("TNotebook.Tab", background="#333333", foreground="#ffffff")

        self.root.configure(bg="#222222")

        self.current_srt_path: Optional[str] = None
        self.original_base: Optional[str] = None
        self.source_lang_code: Optional[str] = None
        self.tab_text_widgets: List[tk.Text] = []
        # Keep frames and expected IDs aligned with tab_text_widgets
        self.tab_frames: List[ttk.Frame] = []
        self.tab_expected_ids: List[List[str]] = []
        # Store translations from tabs the user closes (so rebuild still works)
        self.saved_translations: Dict[str, str] = {}
        dir_frame = ttk.Frame(root)
        dir_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)

        ttk.Label(dir_frame, text="Folder:").pack(side=tk.LEFT)

        self.dir_var = tk.StringVar(value=self.current_dir)
        self.dir_label = ttk.Label(dir_frame, textvariable=self.dir_var)
        self.dir_label.pack(side=tk.LEFT, padx=5)

        self.change_dir_button = ttk.Button(
            dir_frame, text="Change Folder…", command=self.change_folder
        )
        self.change_dir_button.pack(side=tk.LEFT, padx=5)

        file_frame = ttk.Frame(root)
        file_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)

        ttk.Label(
            file_frame, text="Select <name>.<lang>.srt (lang = en, fr, es):"
        ).pack(side=tk.LEFT)

        self.src_srt_files = find_source_srt_files(self.current_dir)
        self.srt_var = tk.StringVar()

        self.srt_combo = ttk.Combobox(
            file_frame,
            textvariable=self.srt_var,
            values=self.src_srt_files,
            state="readonly",
            width=50,
        )
        self.srt_combo.pack(side=tk.LEFT, padx=5)

        if self.src_srt_files:
            self.srt_combo.current(0)
        else:
            self.srt_combo.set("No <name>.<lang>.srt files (en/fr/es) found")

        self.load_button = ttk.Button(
            file_frame, text="Load & Extract", command=self.load_and_extract
        )
        self.load_button.pack(side=tk.LEFT, padx=5)

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=5, pady=5)

        bottom_frame = ttk.Frame(root)
        bottom_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=5)

        self.rebuild_button = ttk.Button(
            bottom_frame,
            text="Rebuild Arabic SRT (.ar.srt)",
            command=self.rebuild_srt_only,
        )
        self.rebuild_button.pack(side=tk.LEFT, padx=5)

        self.bilingual_ass_button = ttk.Button(
            bottom_frame,
            text="Bilingual ASS",
            command=self.create_bilingual_ass_file,
        )
        self.bilingual_ass_button.pack(side=tk.LEFT, padx=5)

        self.arabic_only_ass_button = ttk.Button(
            bottom_frame,
            text="Arabic-only ASS",
            command=self.create_arabic_only_ass_file,
        )
        self.arabic_only_ass_button.pack(side=tk.LEFT, padx=5)

        self.open_vlc_button = ttk.Button(
            bottom_frame,
            text="Open Video in VLC",
            command=self.open_video_only,
        )
        self.open_vlc_button.pack(side=tk.LEFT, padx=5)

        self.close_button = ttk.Button(
            bottom_frame,
            text="Close",
            command=self.root.destroy,
        )
        self.close_button.pack(side=tk.LEFT, padx=5)

        self.status_var = tk.StringVar()
        self.status_var.set("Ready.")
        self.status_label = ttk.Label(bottom_frame, textvariable=self.status_var)
        self.status_label.pack(side=tk.LEFT, padx=10)

    def change_folder(self):
        new_dir = filedialog.askdirectory(initialdir=self.current_dir)
        if not new_dir:
            return

        self.current_dir = new_dir
        self.dir_var.set(self.current_dir)

        self.current_srt_path = None
        self.original_base = None
        self.source_lang_code = None
        self.clear_tabs()
        self.status_var.set("Folder changed. Please select a subtitle file.")

        self.src_srt_files = find_source_srt_files(self.current_dir)
        self.srt_combo["values"] = self.src_srt_files
        if self.src_srt_files:
            self.srt_combo.current(0)
        else:
            self.srt_combo.set("No <name>.<lang>.srt files (en/fr/es) found")

    def clear_tabs(self):
        for tab_id in self.notebook.tabs():
            self.notebook.forget(tab_id)
        self.tab_text_widgets.clear()
        self.tab_frames.clear()
        self.tab_expected_ids.clear()
        self.saved_translations.clear()

    def load_and_extract(self):
        try:
            selected = self.srt_var.get()
            if not selected or selected not in self.src_srt_files:
                messagebox.showerror(
                    "Error",
                    "Please select a valid <name>.<lang>.srt file (lang = en, fr, es) from the dropdown.",
                )
                return

            self.current_srt_path = os.path.join(self.current_dir, selected)
            filename = os.path.basename(self.current_srt_path)

            parts = filename.rsplit(".", 2)
            if len(parts) != 3:
                messagebox.showerror(
                    "Error",
                    f"File '{filename}' does not match '<original_name>.<lang>.srt' pattern.",
                )
                return

            base, lang_code, ext = parts
            if ext != "srt" or lang_code not in SUPPORTED_LANG_CODES:
                messagebox.showerror(
                    "Error",
                    f"File '{filename}' does not match supported pattern '<original_name>.<lang>.srt' "
                    f"with lang in {SUPPORTED_LANG_CODES}.",
                )
                return

            self.original_base = base
            self.source_lang_code = lang_code

            self.status_var.set(f"Loading and extracting from {selected}...")

            lines_with_ids = extract_text_lines_with_ids(self.current_srt_path)
            if not lines_with_ids:
                messagebox.showerror("Error", "No subtitle text lines found in this SRT.")
                self.status_var.set("No subtitle lines found.")
                return

            chunks = split_into_chunks_by_lines(lines_with_ids, MAX_LINES_PER_CHUNK)

            self.clear_tabs()

            for idx, chunk in enumerate(chunks, start=1):
                tab_title = f"Chunk {idx}"
                tab_frame = ttk.Frame(self.notebook)
                self.notebook.add(tab_frame, text=tab_title)

                self.tab_frames.append(tab_frame)

                btn_frame = ttk.Frame(tab_frame)
                btn_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)

                text_widget = tk.Text(tab_frame, wrap="word", undo=True)
                text_widget.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=5, pady=5)

                text_widget.configure(
                    font=("DejaVu Sans Mono", 12),
                    bg="#111111",
                    fg="#ffffff",
                    insertbackground="#ffffff",
                )
                initial_text = PROMPT_TEXT + "\n\n### START LINES\n" + "\n".join(chunk) + "\n### END LINES\n"
                text_widget.insert("1.0", initial_text)

                ttk.Button(btn_frame, text="Copy", command=lambda tw=text_widget: self.copy_text(tw)).pack(side=tk.LEFT, padx=4)
                ttk.Button(btn_frame, text="Erase", command=lambda tw=text_widget: self.erase_text(tw)).pack(side=tk.LEFT, padx=4)
                ttk.Button(btn_frame, text="Paste", command=lambda tw=text_widget: self.paste_text(tw)).pack(side=tk.LEFT, padx=4)

                expected_ids = [ln.split("|", 1)[0] for ln in chunk]
                ttk.Button(
                    btn_frame,
                    text="Validate",
                    command=lambda tw=text_widget, ids=expected_ids, title=tab_title: self.validate_tab(tw, ids, title),
                ).pack(side=tk.LEFT, padx=4)

                ttk.Button(
                    btn_frame,
                    text="Copy Drift-Check Prompt",
                    command=self.copy_drift_check_prompt,
                ).pack(side=tk.LEFT, padx=4)

                ttk.Button(
                    btn_frame,
                    text="Close Tab",
                    command=lambda tf=tab_frame: self.close_tab(tf),
                ).pack(side=tk.LEFT, padx=4)

                self.tab_text_widgets.append(text_widget)
                self.tab_expected_ids.append(expected_ids)

            self.status_var.set(
                f"Extracted {len(lines_with_ids)} lines into {len(chunks)} chunk(s). "
                f"Source language code: {self.source_lang_code}"
            )

            messagebox.showinfo(
                "Extraction complete",
                "Chunks are ready.\n\nFor each tab:\n"
                "- Click 'Copy' and paste into a NEW ChatGPT chat.\n"
                "- Let ChatGPT translate according to the built-in prompt.\n"
                "- Copy ChatGPT's output and use 'Erase' then 'Paste' to replace the content.",
            )

        except Exception as e:
            messagebox.showerror("Error during extraction", str(e))
            self.status_var.set("Error during extraction.")

    def copy_text(self, text_widget: tk.Text):
        content = text_widget.get("1.0", tk.END)
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self.status_var.set("Chunk copied to clipboard.")


    def copy_drift_check_prompt(self):
        """
        Copy a ChatGPT prompt that checks for semantic drift between the source SRT
        (<name>.<lang>.srt) and the generated Arabic SRT (<name>.ar.srt).

        The user should attach BOTH files to the ChatGPT message along with the prompt.
        """
        # Best-effort filenames (the user will attach the actual files in ChatGPT).
        if self.original_base and self.source_lang_code:
            original_name = f"{self.original_base}.{self.source_lang_code}.srt"
            arabic_name = f"{self.original_base}.ar.srt"
        else:
            original_name = "<original_name>.<lang>.srt"
            arabic_name = "<original_name>.ar.srt"

        prompt = DRIFT_CHECK_PROMPT_TEXT.format(
            original_srt=original_name,
            arabic_srt=arabic_name,
        )

        self.root.clipboard_clear()
        self.root.clipboard_append(prompt)
        self.status_var.set("Drift-check prompt copied. Attach both SRT files in ChatGPT and paste.")

    def erase_text(self, text_widget: tk.Text):
        text_widget.delete("1.0", tk.END)
        self.status_var.set("Chunk erased.")

    def paste_text(self, text_widget: tk.Text):
        try:
            content = self.root.clipboard_get()
        except tk.TclError:
            messagebox.showerror("Clipboard error", "Clipboard is empty or not accessible.")
            return
        text_widget.delete("1.0", tk.END)
        text_widget.insert("1.0", content)
        self.status_var.set("Pasted clipboard content into chunk.")
    def validate_tab(self, text_widget: tk.Text, expected_ids: List[str], title: str = ""):
        """
        Validate that the pasted model output contains exactly one translated line per expected ID.
        Only lines of the form: L000001|... are considered.
        """
        raw_lines = text_widget.get("1.0", tk.END).splitlines()

        seen_order: List[str] = []
        id_to_text: Dict[str, str] = {}
        duplicates: List[str] = []

        for raw in raw_lines:
            line = raw.strip()
            if not line or "|" not in line:
                continue

            id_part, text_part = line.split("|", 1)
            line_id = id_part.strip()

            if not LINE_ID_RE.fullmatch(line_id):
                continue

            if line_id in id_to_text:
                duplicates.append(line_id)
                continue

            seen_order.append(line_id)
            id_to_text[line_id] = text_part.rstrip()

        expected_set = set(expected_ids)
        found_set = set(id_to_text.keys())

        missing = [i for i in expected_ids if i not in found_set]
        extra = [i for i in id_to_text.keys() if i not in expected_set]
        empty = [i for i in expected_ids if i in found_set and not id_to_text[i].strip()]

        order_matches = False
        if len(seen_order) == len(expected_ids) and not duplicates and not extra and not missing:
            order_matches = (seen_order == expected_ids)

        problems: List[str] = []
        if missing:
            problems.append("Missing IDs (first 30): " + ", ".join(missing[:30]))
        if extra:
            problems.append("Unexpected IDs (first 30): " + ", ".join(extra[:30]))
        if duplicates:
            problems.append("Duplicate IDs (first 30): " + ", ".join(duplicates[:30]))
        if empty:
            problems.append("Empty translations (first 30): " + ", ".join(empty[:30]))
        if not order_matches and not (missing or extra or duplicates):
            problems.append("Note: ID order differs from the expected order (this is OK for rebuild-by-ID).")

        header = f"Validation: {title}" if title else "Validation"
        if problems:
            messagebox.showwarning(header, "\n\n".join(problems))
            self.status_var.set(f"Validation warnings in {title or 'chunk'}.")
        else:
            messagebox.showinfo(header, "OK — all IDs are present exactly once and translations are non-empty.")
            self.status_var.set(f"Validation OK in {title or 'chunk'}.")

    def close_tab(self, tab_frame: ttk.Frame):
        """
        Close a chunk tab to reduce the number of open tabs.

        Important behavior:
        - Any translated lines found in that tab (L000001|...) are saved into
          self.saved_translations so the final rebuild still works after closing tabs.
        - If the tab looks incomplete (missing IDs, duplicates, etc.), you will be warned.
        """
        try:
            if tab_frame not in self.tab_frames:
                return

            idx = self.tab_frames.index(tab_frame)
            text_widget = self.tab_text_widgets[idx]
            expected_ids = self.tab_expected_ids[idx] if idx < len(self.tab_expected_ids) else []

            raw_lines = text_widget.get("1.0", tk.END).splitlines()

            id_to_text: Dict[str, str] = {}
            duplicates: List[str] = []

            for raw in raw_lines:
                line = raw.strip()
                if not line or "|" not in line:
                    continue

                id_part, text_part = line.split("|", 1)
                line_id = id_part.strip()

                if not LINE_ID_RE.fullmatch(line_id):
                    continue

                if line_id in id_to_text:
                    duplicates.append(line_id)
                    continue

                id_to_text[line_id] = text_part.rstrip()

            expected_set = set(expected_ids)
            found_set = set(id_to_text.keys())

            missing = [i for i in expected_ids if i not in found_set]
            extra = [i for i in id_to_text.keys() if expected_set and i not in expected_set]
            empty = [i for i in expected_ids if i in id_to_text and not id_to_text[i].strip()]

            # Persist whatever we have (non-empty) so closing a tab doesn't lose progress
            overwrote: List[str] = []
            saved_count = 0
            for k, v in id_to_text.items():
                if not v.strip():
                    continue
                if k in self.saved_translations and self.saved_translations[k].strip() and self.saved_translations[k].strip() != v.strip():
                    overwrote.append(k)
                self.saved_translations[k] = v
                saved_count += 1

            problems: List[str] = []
            if missing:
                problems.append(f"Missing IDs in this tab: {len(missing)} (first 20: {', '.join(missing[:20])})")
            if duplicates:
                problems.append(f"Duplicate IDs in this tab: {len(duplicates)} (first 20: {', '.join(duplicates[:20])})")
            if extra:
                problems.append(f"Unexpected IDs in this tab: {len(extra)} (first 20: {', '.join(extra[:20])})")
            if empty:
                problems.append(f"Empty translations in this tab: {len(empty)} (first 20: {', '.join(empty[:20])})")
            if overwrote:
                problems.append(f"Overwrote previously saved IDs: {len(overwrote)} (first 20: {', '.join(overwrote[:20])})")

            if problems:
                msg = (
                    "This tab has issues:\n\n"
                    + "\n".join(problems)
                    + "\n\nTranslated lines found were saved, but missing lines will still be missing for rebuild.\n"
                      "Close this tab anyway?"
                )
                if not messagebox.askyesno("Close Tab", msg):
                    return

            # Actually close the tab and remove it from our tracking lists
            self.notebook.forget(tab_frame)
            self.tab_frames.pop(idx)
            self.tab_text_widgets.pop(idx)
            if idx < len(self.tab_expected_ids):
                self.tab_expected_ids.pop(idx)

            self.status_var.set(f"Closed tab. Saved {saved_count} line(s) from it.")
        except Exception as e:
            messagebox.showerror("Close Tab error", str(e))

    def gather_translations_map(self) -> Dict[str, str]:
        """
        Collect translated lines from:
        - self.saved_translations (tabs the user already closed)
        - all currently open tabs

        Only lines that match ^L\\d{6}$ before the pipe are accepted.

        Duplicate IDs inside the OPEN tabs are treated as an error (because it usually
        indicates the model echoed / duplicated output lines). Duplicate IDs between
        saved_translations and open tabs are allowed; open tabs take precedence.
        """
        out: Dict[str, str] = dict(self.saved_translations)
        duplicates_open: List[str] = []
        seen_open: set[str] = set()

        for text_widget in self.tab_text_widgets:
            for raw in text_widget.get("1.0", tk.END).splitlines():
                line = raw.strip()
                if not line or "|" not in line:
                    continue

                id_part, text_part = line.split("|", 1)
                line_id = id_part.strip()

                if not LINE_ID_RE.fullmatch(line_id):
                    continue

                if line_id in seen_open:
                    duplicates_open.append(line_id)
                    continue

                seen_open.add(line_id)
                out[line_id] = text_part.rstrip()

        if duplicates_open:
            raise RuntimeError(
                "Duplicate IDs found in OPEN tabs (first 30): " + ", ".join(duplicates_open[:30]) +
                "\n\nFix them (use Validate) before rebuilding."
            )

        return out


    def rebuild_srt_only(self):
        try:
            if not self.current_srt_path or not self.original_base:
                messagebox.showerror("Error", "No SRT has been loaded. Load a <name>.<lang>.srt (en/fr/es) first.")
                return

            expected_lines = extract_text_lines_with_ids(self.current_srt_path)
            if not expected_lines:
                messagebox.showerror("Error", "No subtitle text lines found in this SRT.")
                return

            expected_ids = [ln.split("|", 1)[0] for ln in expected_lines]
            expected_set = set(expected_ids)

            translations = self.gather_translations_map()
            if not translations:
                messagebox.showerror(
                    "Error",
                    "No translated lines found in the tabs.\n\n"
                    "Make sure you pasted the model output lines in the form:\n"
                    "L000001|Arabic translation",
                )
                self.status_var.set("No translated lines.")
                return

            missing = [i for i in expected_ids if i not in translations]
            extra = [i for i in translations.keys() if i not in expected_set]
            empty = [i for i in expected_ids if i in translations and not translations[i].strip()]

            if missing or extra or empty:
                msg_parts: List[str] = []
                if missing:
                    msg_parts.append("Missing IDs (first 30): " + ", ".join(missing[:30]))
                if extra:
                    msg_parts.append("Unexpected IDs (first 30): " + ", ".join(extra[:30]))
                if empty:
                    msg_parts.append("Empty translations (first 30): " + ", ".join(empty[:30]))

                messagebox.showerror(
                    "Validation failed — cannot rebuild",
                    "\n\n".join(msg_parts) + "\n\n"
                    "Fix the issues in the chunk tabs (use the Validate button), then rebuild again.",
                )
                self.status_var.set("Rebuild blocked: validation failed.")
                return

            # Rebuild in the original ID order (prevents drift even if model output order differs)
            arabic_lines = [translations[i].rstrip() for i in expected_ids]

            out_path = os.path.join(self.current_dir, self.original_base + ".ar.srt")

            self.status_var.set("Rebuilding Arabic SRT (.ar.srt)...")
            rebuild_srt_sequential(self.current_srt_path, arabic_lines, out_path)

            self.status_var.set(f"Rebuilt SRT: {os.path.basename(out_path)}")
            messagebox.showinfo("Rebuild complete", f"Arabic SRT created:\n{out_path}")

        except Exception as e:
            messagebox.showerror("Error during rebuild", str(e))
            self.status_var.set("Error during rebuild.")

    def create_bilingual_ass_file(self):
        try:
            if not self.current_srt_path or not self.original_base:
                messagebox.showerror("Error", "Load a <name>.<lang>.srt (en/fr/es) first.")
                return

            source_srt_path = self.current_srt_path
            arabic_srt_path = os.path.join(self.current_dir, self.original_base + ".ar.srt")
            if not os.path.isfile(arabic_srt_path):
                messagebox.showerror(
                    "Error",
                    f"Arabic SRT not found:\n{arabic_srt_path}\n\nPlease rebuild the Arabic SRT (.ar.srt) first.",
                )
                return

            default_model = "GPT-5.4 Thinking"
            model_text = simpledialog.askstring(
                "Bilingual ASS",
                "Model text to show near the end of the ASS file:",
                initialvalue=default_model,
                parent=self.root,
            )
            if model_text is None:
                self.status_var.set("Bilingual ASS cancelled.")
                return
            model_text = model_text.strip() or default_model

            output_ass_path = os.path.join(self.current_dir, self.original_base + ".bilingual.ass")

            self.status_var.set("Creating bilingual ASS...")
            self.root.update_idletasks()

            create_bilingual_ass(
                source_srt_path=source_srt_path,
                arabic_srt_path=arabic_srt_path,
                output_ass_path=output_ass_path,
                model_text=model_text,
            )

            self.status_var.set(f"Created bilingual ASS: {os.path.basename(output_ass_path)}")
            messagebox.showinfo(
                "Bilingual ASS created",
                f"Created bilingual ASS file:\n{output_ass_path}\n\n"
                f"Source SRT:\n{source_srt_path}\n\n"
                f"Arabic SRT:\n{arabic_srt_path}",
            )

            delete_srts = messagebox.askyesno(
                "Delete SRT files?",
                "The bilingual ASS file was created successfully.\n\n"
                "Do you want to delete both SRT files?\n\n"
                f"Source SRT:\n{source_srt_path}\n\n"
                f"Arabic SRT:\n{arabic_srt_path}",
            )

            if delete_srts:
                deleted_paths = []
                missing_paths = []
                failed_paths = []

                for srt_path in (source_srt_path, arabic_srt_path):
                    try:
                        if os.path.isfile(srt_path):
                            os.remove(srt_path)
                            deleted_paths.append(srt_path)
                        else:
                            missing_paths.append(srt_path)
                    except Exception as delete_error:
                        failed_paths.append((srt_path, str(delete_error)))

                if failed_paths:
                    details = "\n\n".join(
                        f"{file_path}\n{error_msg}" for file_path, error_msg in failed_paths
                    )
                    self.status_var.set("Bilingual ASS created, but some SRT files could not be deleted.")
                    messagebox.showwarning(
                        "Delete SRT files",
                        "The bilingual ASS file was created, but some SRT files could not be deleted.\n\n"
                        + details,
                    )
                else:
                    details = []
                    if deleted_paths:
                        details.append("Deleted files:\n" + "\n".join(deleted_paths))
                    if missing_paths:
                        details.append("Already missing:\n" + "\n".join(missing_paths))

                    self.status_var.set("Bilingual ASS created and SRT files deleted.")
                    messagebox.showinfo(
                        "Delete SRT files",
                        "SRT cleanup complete.\n\n" + "\n\n".join(details),
                    )
            else:
                self.status_var.set("Created bilingual ASS and kept both SRT files.")

        except Exception as e:
            messagebox.showerror("Error creating bilingual ASS", str(e))
            self.status_var.set("Error creating bilingual ASS.")

    def create_arabic_only_ass_file(self):
        try:
            if not self.current_srt_path or not self.original_base:
                messagebox.showerror("Error", "Load a <name>.<lang>.srt (en/fr/es) first.")
                return

            source_srt_path = self.current_srt_path
            arabic_srt_path = os.path.join(self.current_dir, self.original_base + ".ar.srt")
            if not os.path.isfile(arabic_srt_path):
                messagebox.showerror(
                    "Error",
                    f"Arabic SRT not found:\n{arabic_srt_path}\n\nPlease rebuild the Arabic SRT (.ar.srt) first.",
                )
                return

            default_model = "GPT-5.4 Thinking"
            model_text = simpledialog.askstring(
                "Arabic-only ASS",
                "Model text to show near the end of the ASS file:",
                initialvalue=default_model,
                parent=self.root,
            )
            if model_text is None:
                self.status_var.set("Arabic-only ASS cancelled.")
                return
            model_text = model_text.strip() or default_model

            output_ass_path = os.path.join(self.current_dir, self.original_base + ".arabic-only.ass")

            self.status_var.set("Creating Arabic-only ASS...")
            self.root.update_idletasks()

            create_arabic_only_ass(
                arabic_srt_path=arabic_srt_path,
                output_ass_path=output_ass_path,
                model_text=model_text,
            )

            self.status_var.set(f"Created Arabic-only ASS: {os.path.basename(output_ass_path)}")
            messagebox.showinfo(
                "Arabic-only ASS created",
                f"Created Arabic-only ASS file:\n{output_ass_path}\n\nArabic SRT:\n{arabic_srt_path}",
            )

            delete_srts = messagebox.askyesno(
                "Delete SRT files?",
                "The Arabic-only ASS file was created successfully.\n\n"
                "Do you want to delete both SRT files?\n\n"
                f"Source SRT:\n{source_srt_path}\n\n"
                f"Arabic SRT:\n{arabic_srt_path}",
            )

            if delete_srts:
                deleted_paths = []
                missing_paths = []
                failed_paths = []

                for srt_path in (source_srt_path, arabic_srt_path):
                    try:
                        if os.path.isfile(srt_path):
                            os.remove(srt_path)
                            deleted_paths.append(srt_path)
                        else:
                            missing_paths.append(srt_path)
                    except Exception as delete_error:
                        failed_paths.append((srt_path, str(delete_error)))

                if failed_paths:
                    details = "\n\n".join(
                        f"{file_path}\n{error_msg}" for file_path, error_msg in failed_paths
                    )
                    self.status_var.set("Arabic-only ASS created, but some SRT files could not be deleted.")
                    messagebox.showwarning(
                        "Delete SRT files",
                        "The Arabic-only ASS file was created, but some SRT files could not be deleted.\n\n"
                        + details,
                    )
                else:
                    details = []
                    if deleted_paths:
                        details.append("Deleted files:\n" + "\n".join(deleted_paths))
                    if missing_paths:
                        details.append("Already missing:\n" + "\n".join(missing_paths))

                    self.status_var.set("Arabic-only ASS created and SRT files deleted.")
                    messagebox.showinfo(
                        "Delete SRT files",
                        "SRT cleanup complete.\n\n" + "\n\n".join(details),
                    )
            else:
                self.status_var.set("Created Arabic-only ASS and kept both SRT files.")

        except Exception as e:
            messagebox.showerror("Error creating Arabic-only ASS", str(e))
            self.status_var.set("Error creating Arabic-only ASS.")


    def open_video_only(self):
        try:
            if not self.original_base:
                messagebox.showerror("Error", "No <original_name> available. Load a <name>.<lang>.srt (en/fr/es) first.")
                return

            video_path = find_video_for_base(self.original_base, self.current_dir)
            if video_path:
                open_video_in_vlc(video_path)
            else:
                messagebox.showwarning(
                    "Video not found",
                    f"No video file found for base name '{self.original_base}' in:\n"
                    f"{self.current_dir}\n\n"
                    f"Looked for: {', '.join(self.original_base + ext for ext in VIDEO_EXTENSIONS)}",
                )

        except Exception as e:
            messagebox.showerror("Error opening video", str(e))


def main():
    root = tk.Tk(className=APP_CLASS_NAME)
    apply_app_icon(root, ["srt_translator.png", "srt-translator.png"])
    app = SRTTranslatorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()

