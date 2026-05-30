#!/usr/bin/env python3
"""
macOS GUI book utilities.

Features:
1. JPG to TXT OCR Chunker
   - Select a folder containing images.
   - Extract text with Apple's built-in Vision OCR.
   - Write token-safe .txt chunks into the same selected folder.

2. PDF to TXT Chunker
   - Select a folder containing PDFs.
   - Handles all PDFs in alphabetical/natural filename order.
   - Extracts copyable PDF text directly, and falls back to OCR for scanned pages.
   - Writes token-safe .txt output files into the same selected folder.
   - Keeps each source PDF separate; output text files never mix multiple PDFs.

3. PDF Splitter
   - Select one PDF file from any local/cloud-mounted location, including Drive.
   - Add as many page ranges as needed with the + button.
   - Each part is saved into the same folder as the selected PDF.

4. TXT Cleaner
   - Select a folder containing TXT files.
   - Removes metadata lines containing configured markers.
   - Removes printed page numbers around generated page markers.
   - Joins paragraphs that were split only by generated page markers.
   - Optionally removes a user-provided literal string of any length.
   - Optionally removes title-based running headers such as 'The Hidden Evil  8'.
   - Also removes one empty line immediately following each removed metadata line.

Dependency handling:
- Checks missing modules.
- Installs missing Python packages idempotently and automatically.
"""

from __future__ import annotations

import difflib
import importlib.util
import json
import platform
import re
import subprocess
import sys
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


DEFAULT_MODEL_TOKEN_CAPACITY = 25_000
OUTPUT_PREFIX = "chatgpt_ocr_chunk"
ERRORS_FILENAME = "chatgpt_ocr_errors.txt"
MANIFEST_FILENAME = "chatgpt_ocr_manifest.json"
PDF_OUTPUT_SUFFIX = "part"
PDF_TEXT_OUTPUT_PREFIX = "chatgpt_pdf"
PDF_TEXT_ERRORS_FILENAME = "chatgpt_pdf_errors.txt"
PDF_TEXT_MANIFEST_FILENAME = "chatgpt_pdf_manifest.json"
TXT_CLEAN_METADATA_LINE_RE = re.compile(
    r"^\s*###\s+(?:Source|Page)\b.*$",
    re.IGNORECASE,
)
TXT_CLEAN_PAGE_MARKER_RE = re.compile(
    r"^\s*###\s+Page\b.*$",
    re.IGNORECASE,
)
TXT_CLEAN_STANDALONE_PAGE_NUMBER_RE = re.compile(r"^\s*\d{1,5}\s*$")
TXT_CLEAN_LEADING_PAGE_NUMBER_RE = re.compile(
    r"^\s*(?P<page_number>[0-9٠-٩gqOoIl|]{1,5})\s+(?P<body>\S.*)$"
)
TXT_CLEAN_TRAILING_PAGE_NUMBER_RE = re.compile(
    r"^(?P<body>.*\S)\s+(?P<page_number>\d{1,5})\s*$"
)
TXT_CLEAN_NUMBERED_RUNNING_HEADER_RE = re.compile(
    r"""
    ^\s*
    # OCR sometimes reads a low page number as a letter, e.g.
    # "9. On Advanced Lovemaking" -> "g. On Advanced Lovemaking".
    (?P<page_number>[0-9٠-٩gqOoIl|]{1,5})
    (?:
        \s*[.\-–—•·∙●▪■*:;|\)\]\}]\s*
        |
        \s+
    )
    (?P<title>\S(?:.*\S)?)
    \s*$
    """,
    re.VERBOSE,
)
TXT_CLEAN_TITLE_FIRST_RUNNING_HEADER_RE = re.compile(
    r"""
    ^\s*
    (?P<title>\S(?:.*?\S)?)
    \s+
    # Title-first running headers often look like "Starters 55-" or
    # "Starters 91." in direct PDF text extraction.
    (?P<page_number>[0-9٠-٩gqOoIl|]{1,5})
    \s*[.\-–—•·∙●▪■*:;|\)\]\}]?
    \s*$
    """,
    re.VERBOSE,
)
TXT_CLEAN_OCR_PAGE_NUMBER_TRANSLATION = str.maketrans(
    {
        "g": "9",
        "q": "9",
        "O": "0",
        "o": "0",
        "I": "1",
        "l": "1",
        "|": "1",
    }
)
TXT_CLEAN_NUMBERED_HEADER_MAX_TITLE_LENGTH = 90
TXT_CLEAN_SINGLE_NUMBERED_HEADER_MIN_PAGE = 20
TXT_CLEAN_FUZZY_REPEATED_HEADER_TITLE_RATIO = 0.88
TXT_CLEAN_RUNNING_HEADER_PUNCTUATION = r".\-–—•·∙●▪■*:;|\)\]\}"
TXT_CLEAN_RUNNING_HEADER_PUNCTUATION_CLASS = f"[{TXT_CLEAN_RUNNING_HEADER_PUNCTUATION}]"
TXT_CLEAN_KINDLE_PROGRESS_RE = re.compile(
    r"""
    \s*
    (?<!\w)
    (?:\d+\s*%\s*)?
    (?:
        (?:(?:\d+\s*(?:h|hr|hrs|hour|hours))\s*)?
        (?:\d+\s*(?:m|min|mins|minute|minutes))
        |
        (?:\d+\s*(?:h|hr|hrs|hour|hours))
    )
    \s+
    (?:left|lett|lelt|1eft|ieft)
    \s+in\s+chapter
    (?:\s+\d+\s*%?)?
    \s*
    """,
    re.IGNORECASE | re.VERBOSE,
)
TXT_CLEAN_KINDLE_PAGE_FOOTER_RE = re.compile(
    r"""
    \s*
    (?<!\w)
    Page
    \s+
    (?:\d{1,6}|[٠-٩]{1,6})
    \s+
    of
    \s+
    (?:\d{1,6}|[٠-٩]{1,6})
    (?:\s+\d{1,3}\s*%)?
    \s*
    """,
    re.IGNORECASE | re.VERBOSE,
)
TXT_CLEAN_KINDLE_PAGE_FOOTER_INLINE_RE = re.compile(
    r"""
    (?<!\w)
    \s*
    Page
    \s+
    (?:\d{1,6}|[٠-٩]{1,6})
    \s+
    of
    \s+
    (?:\d{1,6}|[٠-٩]{1,6})
    (?:\s+\d{1,3}\s*%)?
    (?=\s|$)
    """,
    re.IGNORECASE | re.VERBOSE,
)
TXT_CLEAN_FOOTER_LINE_PATTERNS = (
    TXT_CLEAN_KINDLE_PAGE_FOOTER_RE,
    re.compile(r"^\s*learning\s+reading\s+speed\b.*$", re.IGNORECASE),
    re.compile(r"^\s*\d+\s*%\s*$", re.IGNORECASE),
)
TXT_CLEAN_INLINE_NOISE_PATTERNS = (
    TXT_CLEAN_KINDLE_PAGE_FOOTER_INLINE_RE,
    TXT_CLEAN_KINDLE_PROGRESS_RE,
    re.compile(r"\s*\blearning\s+reading\s+speed\b.*$", re.IGNORECASE),
    re.compile(r"\s+\d+\s*%\s*$", re.IGNORECASE),
)

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".webp",
}

REQUIRED_MODULES = {
    "Vision": "pyobjc-framework-Vision",
    "Foundation": "pyobjc-framework-Cocoa",
    "tiktoken": "tiktoken",
    "pypdf": "pypdf",
    "fitz": "PyMuPDF",
}

SENTENCE_END_RE = re.compile(r"[.!?؟。！？…]['\")\]]*$")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?؟。！？…])\s+")
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Scanned/searchable PDFs often contain a full-page image plus an invisible OCR
# text layer. Some OCR layers are badly ordered or incomplete but still contain
# enough letters to pass plain text-quality checks, so we treat full-page image +
# hidden/synthetic text as image-first and re-OCR it with Apple Vision.
PDF_LARGE_IMAGE_AREA_RATIO = 0.60
PDF_SUSPICIOUS_TEXT_LAYER_RATIO = 0.80

# OCR/PDF extraction can accidentally merge marginal notes with the main body
# when two independent text regions share the same vertical position. These
# constants keep left-side notes/captions in their own blocks instead of joining
# them into the right-side sentence stream. Values are normalized page units for
# Vision OCR coordinates.
LAYOUT_MARGIN_X_GAP = 0.045
LAYOUT_MARGIN_MIN_MAIN_TEXT_CHARS = 24
LAYOUT_BLOCK_VERTICAL_GAP_FACTOR = 1.65
LAYOUT_MARGIN_VERTICAL_GAP_FACTOR = 3.25

# Side captions/marginalia in illustrated books often contain cue words such as
# "opposite" or "overleaf". They should be kept, but they must not interrupt a
# paragraph that continues onto the next PDF page. The TXT cleaner uses these
# patterns to move an interrupting side caption after the paragraph it annotates.
TXT_CLEAN_SIDE_NOTE_CUE_WORDS = {
    "opposite",
    "overleaf",
    "above",
    "below",
}
TXT_CLEAN_SIDE_NOTE_CUE_RE = re.compile(
    r"\b(?:opposite|overleaf|above|below|facing\s+page|plate|fig\.?|figure)\b",
    re.IGNORECASE,
)
TXT_CLEAN_MAX_SIDE_NOTE_CHARS = 360


@dataclass(frozen=True)
class OCRLine:
    text: str
    x: float
    top: float
    bottom: float
    height: float


@dataclass(frozen=True)
class ProcessingResult:
    output_files: list[Path]
    processed_images: int
    skipped_images: int
    token_budget: int
    errors: list[str]


@dataclass(frozen=True)
class PDFPartRange:
    start_page: int
    end_page: int


@dataclass(frozen=True)
class PDFSplitResult:
    output_files: list[Path]
    source_pdf: Path
    total_pages: int


@dataclass(frozen=True)
class PDFTextResult:
    output_files: list[Path]
    processed_pdfs: int
    skipped_pdfs: int
    processed_pages: int
    direct_text_pages: int
    ocr_pages: int
    token_budget: int
    errors: list[str]


@dataclass(frozen=True)
class TextCleanResult:
    cleaned_files: list[Path]
    scanned_files: int
    changed_files: int
    removed_marker_lines: int
    removed_empty_lines: int
    errors: list[str]


class BookUtilsError(RuntimeError):
    """Raised for user-facing failures."""


class OCRChunkerError(BookUtilsError):
    """Raised for user-facing OCR chunker failures."""


class PDFSplitterError(BookUtilsError):
    """Raised for user-facing PDF splitter failures."""


class PDFTextExtractionError(BookUtilsError):
    """Raised for user-facing PDF text extraction failures."""


class TextCleanerError(BookUtilsError):
    """Raised for user-facing TXT cleaner failures."""


def in_virtualenv() -> bool:
    """Return True when running inside a virtual environment."""
    return (
        getattr(sys, "base_prefix", sys.prefix) != sys.prefix
        or getattr(sys, "real_prefix", None) is not None
    )


def install_package(package_name: str) -> None:
    """Install one package into the active interpreter.

    Important: do not use --user inside a virtualenv. macOS/Automator launchers
    often run this script from .ocr_venv, where --user installs are disabled.
    """
    base_cmd = [sys.executable, "-m", "pip", "install", "--upgrade"]

    if in_virtualenv():
        cmd = [*base_cmd, package_name]
    else:
        cmd = [*base_cmd, "--user", package_name]

    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError:
        # Fallback for Homebrew/externally-managed Python setups. If --user failed,
        # try the active interpreter environment without --user before giving up.
        if "--user" in cmd:
            subprocess.check_call([*base_cmd, package_name])
        else:
            raise


def ensure_dependencies() -> None:
    """Install missing dependencies only when needed."""
    if platform.system() != "Darwin":
        raise BookUtilsError(
            "This script is macOS-only because the OCR feature uses Apple Vision OCR."
        )

    missing_packages: list[str] = []

    for module_name, package_name in REQUIRED_MODULES.items():
        if importlib.util.find_spec(module_name) is None:
            missing_packages.append(package_name)

    if not missing_packages:
        return

    ensure_pip()

    for package_name in sorted(set(missing_packages)):
        install_package(package_name)

    importlib.invalidate_caches()


def ensure_pip() -> None:
    """Ensure pip exists for the current Python interpreter."""
    if importlib.util.find_spec("pip") is not None:
        return

    subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])


def natural_sort_key(path: Path) -> list[object]:
    """Sort files like page_2.jpg before page_10.jpg."""
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", path.name)
    ]


def find_images(folder: Path) -> list[Path]:
    """Return supported image files in natural filename order."""
    return sorted(
        [
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        ],
        key=natural_sort_key,
    )


def find_pdfs(folder: Path) -> list[Path]:
    """Return PDF files in alphabetical/natural filename order."""
    return sorted(
        [
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() == ".pdf"
        ],
        key=natural_sort_key,
    )


def find_txt_files(folder: Path) -> list[Path]:
    """Return TXT files in alphabetical/natural filename order."""
    return sorted(
        [
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() == ".txt"
        ],
        key=natural_sort_key,
    )


def line_is_standalone_clean_marker(line: str) -> bool:
    """Return True only for standalone generated metadata/footer lines.

    Important: this must not use loose substring matching. OCR can sometimes
    merge Kindle footer text with a real paragraph, so deleting any line that
    merely contains "left in chapter" can erase valid book text.
    """
    normalized_line = line.replace("\u00a0", " ").strip()

    return (
        bool(TXT_CLEAN_METADATA_LINE_RE.match(normalized_line))
        or bool(TXT_CLEAN_KINDLE_PROGRESS_RE.fullmatch(normalized_line))
        or any(
            pattern.fullmatch(normalized_line)
            for pattern in TXT_CLEAN_FOOTER_LINE_PATTERNS
        )
    )


def line_is_generated_page_marker(line: str) -> bool:
    """Return True for generated page headers such as '### Page 2'."""
    normalized_line = line.replace("\u00a0", " ").strip()
    return bool(TXT_CLEAN_PAGE_MARKER_RE.match(normalized_line))


def line_is_standalone_page_number(line: str) -> bool:
    """Return True for a printed page number on its own line."""
    normalized_line = line.replace("\u00a0", " ").strip()
    return bool(TXT_CLEAN_STANDALONE_PAGE_NUMBER_RE.fullmatch(normalized_line))


def parse_unicode_page_number(page_number: str) -> int | None:
    """Parse Western, Arabic-Indic, and common OCR-confused page digits."""
    page_number = page_number.translate(TXT_CLEAN_OCR_PAGE_NUMBER_TRANSLATION)

    try:
        return int("".join(str(unicodedata.digit(character)) for character in page_number))
    except (TypeError, ValueError):
        return None


def normalize_numbered_running_header_title(title: str) -> str:
    """Normalize a detected running-header title for repeat/fuzzy checks."""
    title = unicodedata.normalize("NFKC", title.replace("\u00a0", " "))
    title = re.sub(r"[\s_]+", " ", title).strip()
    title = title.strip(" .,-–—•·∙●▪■*:;|()[]{}")
    return title.casefold()


def numbered_running_header_title_looks_like_structural_heading(title: str) -> bool:
    """Return True for real section headings that can follow a printed page number."""
    normalized_title = normalize_numbered_running_header_title(title)
    return bool(
        re.match(
            r"^(?:chapter|chap\.?|book|part|volume|vol\.?)\s+"
            r"(?:\d+|[ivxlcdm]+)\b",
            normalized_title,
        )
    )


def numbered_running_header_parts(line: str) -> tuple[int | None, str, str] | None:
    """Return parts for generic page headers.

    Supported shapes include:
    - "230. The Joy of Sex" / "229- Problems" / "233• Problems"
    - "51 Starters"
    - "Starters 55-" / "Starters 91."

    The title can be anything; the detection is intentionally title-agnostic.
    This helper only identifies the shape. The caller decides whether it is safe
    to remove the line based on page-marker context, page number, and repeated
    titles.
    """
    normalized_line = line.replace("\u00a0", " ").strip()

    match = TXT_CLEAN_NUMBERED_RUNNING_HEADER_RE.fullmatch(normalized_line)
    if match:
        title = match.group("title").strip()
        page_number = parse_unicode_page_number(match.group("page_number"))
    else:
        match = TXT_CLEAN_TITLE_FIRST_RUNNING_HEADER_RE.fullmatch(normalized_line)
        if not match:
            return None

        title = match.group("title").strip()
        page_number = parse_unicode_page_number(match.group("page_number"))

    if len(title) > TXT_CLEAN_NUMBERED_HEADER_MAX_TITLE_LENGTH:
        return None

    # Avoid treating punctuation/garbage-only OCR lines as real headers.
    if not any(character.isalpha() for character in title):
        return None

    normalized_title = normalize_numbered_running_header_title(title)
    if not normalized_title:
        return None

    return page_number, title, normalized_title


def numbered_running_header_prefix_parts(
    line: str,
) -> tuple[int | None, str, str, str] | None:
    """Return header parts when a running header is merged into body text.

    This is intentionally used only for learning repeated folder headers and
    prefix stripping. Standalone detection remains stricter.
    """
    normalized_line = line.replace("\u00a0", " ").strip()
    if not normalized_line:
        return None

    page_number_pattern = r"(?P<page_number>[0-9٠-٩gqOoIl|]{1,5})"
    punct_pattern = TXT_CLEAN_RUNNING_HEADER_PUNCTUATION_CLASS

    # Title-first with a visible separator after the page number:
    # "The Joy of Sex 114• It starts..."
    match = re.match(
        rf"^\s*(?P<title>\S(?:.*?\S)?)\s+{page_number_pattern}"
        rf"\s*{punct_pattern}\s+(?P<body>\S.*)$",
        normalized_line,
        re.IGNORECASE,
    )
    if match:
        title = match.group("title").strip()
        body = match.group("body").strip()
        page_number = parse_unicode_page_number(match.group("page_number"))

        if (
            page_number is not None
            and page_number >= TXT_CLEAN_SINGLE_NUMBERED_HEADER_MIN_PAGE
            and len(title) <= TXT_CLEAN_NUMBERED_HEADER_MAX_TITLE_LENGTH
            and any(character.isalpha() for character in title)
            and line_looks_like_body_text_after_header(body)
        ):
            normalized_title = normalize_numbered_running_header_title(title)
            if normalized_title:
                return page_number, title, normalized_title, body

    # Page-first with a visible separator and a learned-looking title followed
    # by lowercase body text, e.g. "151 Main Courses initially at least...".
    match = re.match(
        rf"^\s*{page_number_pattern}"
        rf"(?:\s*{punct_pattern}\s*|\s+)"
        rf"(?P<rest>\S.*)$",
        normalized_line,
        re.IGNORECASE,
    )
    if not match:
        return None

    page_number = parse_unicode_page_number(match.group("page_number"))
    if page_number is None or page_number < TXT_CLEAN_SINGLE_NUMBERED_HEADER_MIN_PAGE:
        return None

    rest = match.group("rest").strip()
    words = rest.split()
    if len(words) < 3:
        return None

    # Prefer a split before the first clearly lowercase body word, while
    # allowing short lowercase connector words inside titles: "The Joy of Sex".
    connector_words = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with"}
    title_words: list[str] = []
    body_words: list[str] = []

    for word in words:
        stripped_word = word.strip(" .,-–—•·∙●▪■*:;|()[]{}'\"")
        is_lower_body_word = (
            bool(stripped_word)
            and stripped_word[:1].islower()
            and stripped_word.casefold() not in connector_words
            and len(title_words) >= 1
        )
        if is_lower_body_word:
            body_words = words[len(title_words):]
            break
        title_words.append(word)

    if not body_words:
        return None

    title = " ".join(title_words).strip()
    body = " ".join(body_words).strip()
    if (
        not title
        or len(title) > TXT_CLEAN_NUMBERED_HEADER_MAX_TITLE_LENGTH
        or not any(character.isalpha() for character in title)
        or not line_looks_like_body_text_after_header(body)
    ):
        return None

    normalized_title = normalize_numbered_running_header_title(title)
    if not normalized_title:
        return None

    return page_number, title, normalized_title, body


def collect_numbered_running_header_titles(lines: list[str]) -> set[str]:
    """Collect repeated running-header titles after page markers.

    Examples include '230. The Joy of Sex', '229- Problems',
    '233• Problems', '51 Starters', and 'Starters 55-'. Repetition makes
    removal safer because ordinary chapter headings like '1. Introduction'
    often appear only once.
    """
    title_counts: dict[str, int] = {}
    index = 0

    while index < len(lines):
        if not line_is_generated_page_marker(lines[index]):
            index += 1
            continue

        index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1

        if index >= len(lines):
            break

        parts = numbered_running_header_parts(lines[index])
        if parts is not None:
            _, _, normalized_title = parts
            title_counts[normalized_title] = title_counts.get(normalized_title, 0) + 1

    return {title for title, count in title_counts.items() if count >= 2}


def title_is_fuzzy_repeated_numbered_header(
    normalized_title: str,
    repeated_numbered_header_titles: set[str],
) -> bool:
    """Return True when a header title looks like an OCR variant of a repeated one.

    This catches cases such as "The loy of Sex" versus "The Joy of Sex"
    without making low-number cleanup broad enough to delete ordinary headings.
    It is only used for the first non-empty line after a generated page marker.
    """
    if not normalized_title or not repeated_numbered_header_titles:
        return False

    title_words = normalized_title.split()
    if len(normalized_title) < 8 or len(title_words) < 2:
        return False

    for repeated_title in repeated_numbered_header_titles:
        repeated_words = repeated_title.split()
        if len(repeated_title) < 8 or len(repeated_words) < 2:
            continue

        # Require stable outer words so unrelated chapter headings with a
        # coincidentally high similarity score are not removed.
        if title_words[0] != repeated_words[0] or title_words[-1] != repeated_words[-1]:
            continue

        ratio = difflib.SequenceMatcher(
            None,
            normalized_title,
            repeated_title,
        ).ratio()
        if ratio >= TXT_CLEAN_FUZZY_REPEATED_HEADER_TITLE_RATIO:
            return True

    return False


def line_is_contextual_numbered_running_header(
    line: str,
    repeated_numbered_header_titles: set[str],
) -> bool:
    """Return True for removable page running headers after markers.

    Removal is intentionally contextual: clean_txt_content() calls this only for
    the first non-empty line after a generated '### Page ...' marker, or for
    the first non-empty line of an already-cleaned single-page chunk. This avoids
    deleting normal numbered list items inside the body text.

    Low page numbers are removed only when their title is repeated, or when it
    is a close OCR variant of a repeated title. This catches running headers
    such as "g. On Advanced Lovemaking" while preserving real headings like
    "1. Introduction".
    """
    parts = numbered_running_header_parts(line)
    if parts is None:
        return False

    page_number, title, normalized_title = parts
    if numbered_running_header_title_looks_like_structural_heading(title):
        return False

    if normalized_title in repeated_numbered_header_titles:
        return True

    if title_is_fuzzy_repeated_numbered_header(
        normalized_title,
        repeated_numbered_header_titles,
    ):
        return True

    # Keep single low-number headings such as '1. Introduction' by default, but
    # still clean isolated book-page headers in small chunks such as
    # '230. The Joy of Sex' where the title may occur only once.
    return (
        page_number is not None
        and page_number >= TXT_CLEAN_SINGLE_NUMBERED_HEADER_MIN_PAGE
    )


def remove_contextual_numbered_running_header(
    lines: list[str],
    index: int,
    repeated_numbered_header_titles: set[str],
) -> tuple[int, int, int]:
    """Remove a number-first running header at index, plus one following blank.

    Returns the new index, removed marker-line count, and removed empty-line
    count. The caller must ensure index points to the first non-empty line after
    a generated page marker.
    """
    if index >= len(lines) or not line_is_contextual_numbered_running_header(
        lines[index],
        repeated_numbered_header_titles,
    ):
        return index, 0, 0

    index += 1
    removed_marker_lines = 1
    removed_empty_lines = 0

    if index < len(lines) and not lines[index].strip():
        removed_empty_lines += 1
        index += 1

    return index, removed_marker_lines, removed_empty_lines



def strip_contextual_numbered_running_header_prefix(
    line: str,
    repeated_numbered_header_titles: set[str],
) -> tuple[str, bool]:
    """Strip a page running-header prefix from the first line after a page marker.

    This catches OCR/PDF cases where the header and body were merged into one
    line, for example:
        "55- Starters vagina and the genital odor..."
        "151 Main Courses initially at least..."

    It is contextual and conservative: callers should use it only at a page
    boundary or at the very first line of an already-cleaned page chunk. Inline
    prefixes remove the title only when that title was learned as repeated
    header text; otherwise the caller can remove just the fused page number.
    """
    parts = numbered_running_header_prefix_parts(line)
    if parts is None:
        return line, False

    _, _, normalized_title, body = parts
    if not body:
        return "", True

    if not (
        normalized_title in repeated_numbered_header_titles
        or title_is_fuzzy_repeated_numbered_header(
            normalized_title,
            repeated_numbered_header_titles,
        )
    ):
        return line, False

    line_body = line.rstrip("\r\n")
    line_ending = line[len(line_body):]
    return body.strip() + line_ending, True


def line_looks_like_body_text_after_header(text: str) -> bool:
    """Return True when text after/under a header looks like prose, not a title."""
    normalized_text = text.replace("\u00a0", " ").strip()
    if not normalized_text:
        return False

    if numbered_running_header_parts(normalized_text) is not None:
        return False

    if normalized_text[:1].islower():
        return True

    words = normalized_text.split()
    has_lowercase = any(character.islower() for character in normalized_text)
    has_sentence_punctuation = bool(re.search(r"[,.;:!?]", normalized_text))

    return (
        len(normalized_text) >= 35
        and len(words) >= 6
        and has_lowercase
        and (has_sentence_punctuation or not normalized_text.isupper())
    )


def line_is_initial_running_header(
    lines: list[str],
    index: int,
    repeated_numbered_header_titles: set[str],
) -> bool:
    """Return True for a running header at the start of a cleaned chunk.

    This is intentionally narrower than post-page-marker cleanup. It lets a user
    rerun TXT Cleaner on a file that was already cleaned by an older version,
    where the generated page marker is gone but the first line is still a page
    header such as "51 Starters", "Starters 91.", "80 The loy of Sex",
    "The Joy of Sex 114•", or "151 Main Courses".
    """
    if index >= len(lines):
        return False

    parts = numbered_running_header_parts(lines[index])
    if parts is None:
        return False

    page_number, title, normalized_title = parts
    if numbered_running_header_title_looks_like_structural_heading(title):
        return False

    title_is_learned = (
        normalized_title in repeated_numbered_header_titles
        or title_is_fuzzy_repeated_numbered_header(
            normalized_title,
            repeated_numbered_header_titles,
        )
    )

    # At the very beginning of an already-cleaned file we do not have the
    # generated page marker anymore, so require either a learned/repeated title
    # or a high book-page number. This keeps low numbered real headings safer.
    if not title_is_learned and not (
        page_number is not None
        and page_number >= TXT_CLEAN_SINGLE_NUMBERED_HEADER_MIN_PAGE
    ):
        return False

    next_text = next_nonempty_line_text(lines, index + 1)
    return line_looks_like_body_text_after_header(next_text)


def remove_trailing_page_number(line: str) -> tuple[str, bool]:
    """Remove a printed page number appended to the end of a text line.

    This is only called when a generated '### Page ...' marker immediately
    follows, so the trailing number is treated as page-boundary noise rather
    than ordinary text.
    """
    line_body = line.rstrip("\r\n")
    line_ending = line[len(line_body):]
    normalized_body = line_body.replace("\u00a0", " ")
    match = TXT_CLEAN_TRAILING_PAGE_NUMBER_RE.match(normalized_body)

    if not match:
        return line, False

    body_without_page_number = match.group("body").rstrip()

    # Avoid stripping short legitimate headings such as "Chapter 5" when they
    # happen to appear before a generated page marker.
    if len(body_without_page_number) < 40 and not re.search(
        r"[,;:]|\b(?:and|or|but|then|when|while|with|to|in|on|at|for|from)\b\s*$",
        body_without_page_number,
        re.IGNORECASE,
    ):
        return line, False

    return body_without_page_number + line_ending, True


def remove_leading_page_number_after_marker(
    line: str,
    previous_text: str,
) -> tuple[str, bool]:
    """Remove a printed page number fused to the first line after a page marker.

    Direct PDF extraction can emit a top page number and the first text block as
    one line, e.g. "125 I used..." or "124 never got...". This removes only the
    number, leaving the body text intact.
    """
    line_body = line.rstrip("\r\n")
    line_ending = line[len(line_body):]
    normalized_body = line_body.replace("\u00a0", " ")
    match = TXT_CLEAN_LEADING_PAGE_NUMBER_RE.match(normalized_body)
    if not match:
        return line, False

    page_number = parse_unicode_page_number(match.group("page_number"))
    if page_number is None:
        return line, False

    body = match.group("body").strip()
    if not body:
        return line, False

    first_character = first_meaningful_character(body)
    if first_character and first_character.isupper():
        return body + line_ending, True

    if previous_text and should_join_page_boundary(previous_text, body):
        return body + line_ending, True

    return line, False


def last_nonempty_line_text(lines: list[str]) -> str:
    """Return the text from the last non-empty line already kept."""
    for line in reversed(lines):
        text = line.replace("\u00a0", " ").strip()
        if text:
            return text

    return ""


def next_nonempty_line_text(lines: list[str], start_index: int) -> str:
    """Return the next non-empty source line after start_index."""
    for line in lines[start_index:]:
        text = line.replace("\u00a0", " ").strip()
        if text:
            return text

    return ""


def should_join_page_boundary(previous_text: str, next_text: str) -> bool:
    """Return True when a removed page marker split one running paragraph."""
    previous_text = previous_text.strip()
    next_text = next_text.strip()

    if not previous_text or not next_text:
        return False

    if ends_at_safe_boundary(previous_text):
        return False

    if previous_text.endswith(("-", "—", "–")):
        return True

    if next_text[:1].islower():
        return True

    return previous_text.endswith((",", ";", ":", "(", "[", "{"))


def append_cleaned_line(
    cleaned_lines: list[str],
    cleaned_line: str,
    join_with_previous: bool = False,
) -> None:
    """Append a cleaned line, optionally joining it to the previous text line."""
    if not cleaned_line:
        return

    if not join_with_previous or not cleaned_lines:
        cleaned_lines.append(cleaned_line)
        return

    while cleaned_lines and not cleaned_lines[-1].strip():
        cleaned_lines.pop()

    if not cleaned_lines:
        cleaned_lines.append(cleaned_line)
        return

    previous_line = cleaned_lines[-1]
    previous_body = previous_line.rstrip("\r\n")
    cleaned_body = cleaned_line.lstrip().rstrip("\r\n")
    cleaned_ending = cleaned_line[len(cleaned_line.rstrip("\r\n")):]

    separator = "" if previous_body.rstrip().endswith(("-", "—", "–")) else " "
    cleaned_lines[-1] = (
        previous_body.rstrip()
        + separator
        + cleaned_body.strip()
        + cleaned_ending
    )


def remove_terminal_page_number_footer(
    cleaned_lines: list[str],
) -> tuple[int, int]:
    """Remove a final printed page number left at the end of a cleaned TXT file.

    Page-boundary cleanup can remove numbers before generated "### Page ..."
    markers, but the final PDF page has no following generated marker. In those
    cases OCR/PDF extraction can leave a dangling footer like "14" as the last
    line of the whole file.

    To avoid deleting meaningful numbered content, this only removes a standalone
    number when it is the final non-empty line and there is real text before it.
    """
    removed_marker_lines = 0
    removed_empty_lines = 0

    while cleaned_lines and not cleaned_lines[-1].strip():
        cleaned_lines.pop()
        removed_empty_lines += 1

    if not cleaned_lines or not line_is_standalone_page_number(cleaned_lines[-1]):
        return removed_marker_lines, removed_empty_lines

    previous_text = ""
    for previous_line in reversed(cleaned_lines[:-1]):
        previous_text = previous_line.replace("\u00a0", " ").strip()
        if previous_text:
            break

    # Do not turn a file that only contains a number into an empty file.
    if not previous_text:
        return removed_marker_lines, removed_empty_lines

    cleaned_lines.pop()
    removed_marker_lines += 1

    while cleaned_lines and not cleaned_lines[-1].strip():
        cleaned_lines.pop()
        removed_empty_lines += 1

    return removed_marker_lines, removed_empty_lines


def custom_clean_string_has_linebreak(custom_clean_string: str) -> bool:
    """Return True when a custom literal cleanup string spans multiple lines."""
    return "\n" in custom_clean_string or "\r" in custom_clean_string


def normalize_custom_clean_string_for_line_match(custom_clean_string: str) -> str:
    """Normalize a single-line custom cleanup string for standalone line checks."""
    return custom_clean_string.replace("\u00a0", " ").strip()


def line_is_standalone_custom_clean_string(
    line: str,
    custom_clean_string: str,
) -> bool:
    """Return True when a line is exactly the custom literal cleanup string.

    The custom value is treated as plain text, never as a regex, so characters
    such as ., *, [, ], ?, $, or backslashes are matched literally.
    """
    if not custom_clean_string or custom_clean_string_has_linebreak(custom_clean_string):
        return False

    normalized_custom = normalize_custom_clean_string_for_line_match(
        custom_clean_string
    )
    if not normalized_custom:
        return False

    normalized_line = line.replace("\u00a0", " ").strip()
    return normalized_line == normalized_custom


def remove_multiline_custom_clean_string(
    text: str,
    custom_clean_string: str,
) -> tuple[str, int]:
    """Remove a multi-line custom cleanup string as a literal text block."""
    if not custom_clean_string or not custom_clean_string_has_linebreak(custom_clean_string):
        return text, 0

    custom_variants = [custom_clean_string]

    # Tkinter Text widgets use "\n" for newlines. Add common file-line-ending
    # variants so a pasted multi-line marker still matches CRLF/CR text files.
    if "\r" not in custom_clean_string:
        custom_variants.append(custom_clean_string.replace("\n", "\r\n"))
        custom_variants.append(custom_clean_string.replace("\n", "\r"))

    cleaned = text
    removed_count = 0

    for custom_variant in dict.fromkeys(custom_variants):
        if not custom_variant:
            continue
        count = cleaned.count(custom_variant)
        if count:
            cleaned = cleaned.replace(custom_variant, "")
            removed_count += count

    return cleaned, removed_count


def strip_embedded_clean_noise(
    line: str,
    custom_clean_string: str = "",
) -> tuple[str, bool]:
    """Remove footer/custom noise inside a line without deleting real text.

    Example:
    "real paragraph text 1 min left in chapter 6%\n"
    becomes:
    "real paragraph text\n"
    """
    line_body = line.rstrip("\r\n")
    line_ending = line[len(line_body):]

    cleaned = line_body.replace("\u00a0", " ")

    for pattern in TXT_CLEAN_INLINE_NOISE_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)

    if custom_clean_string and not custom_clean_string_has_linebreak(custom_clean_string):
        normalized_custom = custom_clean_string.replace("\u00a0", " ")
        if normalized_custom:
            cleaned = cleaned.replace(normalized_custom, " ")

    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()

    if not cleaned:
        return "", cleaned != line_body.strip()

    return cleaned + line_ending, cleaned != line_body.strip()



def strip_learned_running_header_prefix(
    line: str,
    repeated_numbered_header_titles: set[str],
) -> tuple[str, bool]:
    """Remove learned running-header prefixes even when OCR merged body text.

    Examples:
        "The Joy of Sex 114• It starts here." -> "It starts here."
        "151 Main Courses initially at least..." -> "initially at least..."

    This only uses titles learned as repeated headers in the current file/folder,
    which is safer than stripping arbitrary title-like prefixes.
    """
    if not repeated_numbered_header_titles:
        return line, False

    line_body = line.rstrip("\r\n")
    line_ending = line[len(line_body):]
    normalized_body = line_body.replace("\u00a0", " ")

    page_number_pattern = r"(?:[0-9٠-٩gqOoIl|]{1,5})"
    punct_pattern = TXT_CLEAN_RUNNING_HEADER_PUNCTUATION_CLASS

    # Longer titles first avoids partially stripping a shorter prefix when one
    # header title is a prefix of another.
    for normalized_title in sorted(
        repeated_numbered_header_titles,
        key=len,
        reverse=True,
    ):
        title_pattern = make_flexible_literal_pattern(normalized_title)
        if not title_pattern:
            continue

        title_first_pattern = re.compile(
            rf"^[ \t]*{title_pattern}[ \t]+{page_number_pattern}"
            rf"[ \t]*{punct_pattern}?[ \t]*(?P<body>.*)$",
            re.IGNORECASE,
        )
        page_first_pattern = re.compile(
            rf"^[ \t]*{page_number_pattern}"
            rf"(?:[ \t]*{punct_pattern}[ \t]*|[ \t]+)"
            rf"{title_pattern}[ \t]*{punct_pattern}?[ \t]*(?P<body>.*)$",
            re.IGNORECASE,
        )

        for pattern in (title_first_pattern, page_first_pattern):
            match = pattern.match(normalized_body)
            if not match:
                continue

            body = match.group("body").strip()
            if not body:
                return "", True

            if not line_looks_like_body_text_after_header(body):
                continue

            return body + line_ending, True

    return line, False


def make_flexible_literal_pattern(literal_text: str) -> str:
    """Return a regex fragment for literal text with flexible whitespace.

    Example: "The Hidden Evil" matches "The Hidden Evil" and
    "The   Hidden   Evil", while punctuation and other characters remain
    literal because every non-space chunk is escaped.
    """
    parts = [re.escape(part) for part in literal_text.replace("\u00a0", " ").split()]
    return r"\s+".join(parts)


def strip_running_header_prefix(
    line: str,
    running_header_title: str = "",
) -> tuple[str, bool]:
    """Remove a title + page number running header at the start of a line.

    The title is user-provided and treated as literal text, not as regex.
    This intentionally only removes headers at the beginning of a line, such as:

        "The Hidden Evil  8 They are correct."

    It becomes:

        "They are correct."

    Arabic and other Unicode titles are supported. Page numbers can use
    Western digits or Arabic-Indic digits.
    """
    title = running_header_title.replace("\u00a0", " ").strip()
    if not title:
        return line, False

    title_pattern = make_flexible_literal_pattern(title)
    if not title_pattern:
        return line, False

    line_body = line.rstrip("\r\n")
    line_ending = line[len(line_body):]
    normalized_body = line_body.replace("\u00a0", " ")

    pattern = re.compile(
        rf"^[ \t]*{title_pattern}[ \t]+"
        rf"(?:[0-9]{{1,5}}|[٠-٩]{{1,5}})"
        rf"(?=$|[ \t]+)[ \t]*"
    )

    cleaned_body, count = pattern.subn("", normalized_body, count=1)
    if not count:
        return line, False

    cleaned_body = cleaned_body.lstrip()
    if not cleaned_body:
        return "", True

    return cleaned_body + line_ending, True



def normalize_clean_paragraph_text(paragraph: str) -> str:
    """Return one-line normalized text for paragraph-level cleanup checks."""
    return re.sub(r"\s+", " ", paragraph.replace("\u00a0", " ").strip())


def first_meaningful_character(text: str) -> str:
    """Return the first alphanumeric character after quotes/brackets, if any."""
    for character in text.lstrip(" \t\r\n'\"“”‘’([{<"):
        if character.isalnum():
            return character
    return ""


def paragraph_starts_like_continuation(paragraph: str) -> bool:
    """Return True when a paragraph looks like the continuation of a split one."""
    text = normalize_clean_paragraph_text(paragraph)
    first_character = first_meaningful_character(text)
    return bool(first_character and first_character.islower())


def uppercase_letter_ratio(text: str) -> float:
    """Return the ratio of uppercase letters among cased letters."""
    letters = [character for character in text if character.isalpha()]
    if not letters:
        return 0.0
    uppercase_letters = [character for character in letters if character.isupper()]
    return len(uppercase_letters) / len(letters)


def word_without_trailing_punctuation(word: str) -> str:
    """Normalize a word for cue-word matching."""
    return word.strip(" \t\r\n.,;:!?()[]{}<>•·-*—–").casefold()


def paragraph_looks_like_side_note_caption(paragraph: str) -> bool:
    """Return True for short side captions such as 'BIRTH CONTROL opposite ...'.

    This is intentionally conservative. It detects captions/marginal notes with
    layout cue words, but does not classify ordinary short paragraphs as side
    notes merely because they are short.
    """
    text = normalize_clean_paragraph_text(paragraph)
    if not text or len(text) > TXT_CLEAN_MAX_SIDE_NOTE_CHARS:
        return False

    if not TXT_CLEAN_SIDE_NOTE_CUE_RE.search(text):
        return False

    words = text.split()
    if len(words) < 2:
        return False

    normalized_words = [word_without_trailing_punctuation(word) for word in words]

    for index, normalized_word in enumerate(normalized_words[:8]):
        if normalized_word not in TXT_CLEAN_SIDE_NOTE_CUE_WORDS:
            continue

        # Common shapes:
        #   BIRTH CONTROL opposite The discovery ...
        #   REAL SEX opposite Permissiveness ...
        #   overleaf BEDS Still the most important ...
        if index == 0:
            following_label = " ".join(words[1:4])
            return uppercase_letter_ratio(following_label) >= 0.60

        leading_label = " ".join(words[:index])
        if uppercase_letter_ratio(leading_label) >= 0.60:
            return True

        # Some OCR engines merge a lowercase marginal title with an uppercase
        # caption, e.g. "birth contro BIRTH CONTROL opposite ...". The cue word
        # still makes it caption-like, so allow a slightly lower uppercase ratio
        # when there are multiple words before the cue.
        if index >= 2 and uppercase_letter_ratio(leading_label) >= 0.50:
            return True

    return False


def join_split_paragraph_fragments(previous_paragraph: str, next_paragraph: str) -> str:
    """Join two paragraph fragments split by a page boundary or side note."""
    previous_body = previous_paragraph.rstrip()
    next_body = next_paragraph.lstrip()
    separator = "" if previous_body.endswith(("-", "—", "–")) else " "
    return previous_body + separator + next_body



def words_for_caption_similarity(text: str) -> list[str]:
    """Return normalized words for side-caption/main-text similarity checks."""
    return [
        word.casefold()
        for word in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]{3,}", text)
    ]


def side_note_caption_tail(paragraph: str) -> str:
    """Return the descriptive part after a side-caption cue word."""
    text = normalize_clean_paragraph_text(paragraph)
    match = TXT_CLEAN_SIDE_NOTE_CUE_RE.search(text)
    if not match:
        return text
    return text[match.end():].strip(" .,-–—•·:;")


def side_note_caption_matches_following_paragraph(
    side_note_paragraph: str,
    following_paragraph: str,
) -> bool:
    """Return True when a caption appears to summarize the following paragraph."""
    caption_tail = side_note_caption_tail(side_note_paragraph)
    if len(caption_tail) < 20:
        return False

    caption_words = words_for_caption_similarity(caption_tail)
    following_words = words_for_caption_similarity(following_paragraph)[:28]
    if len(caption_words) < 4 or len(following_words) < 4:
        return False

    caption_set = set(caption_words)
    following_set = set(following_words)
    overlap_ratio = len(caption_set & following_set) / max(1, len(caption_set))
    if overlap_ratio >= 0.45:
        return True

    following_start = " ".join(following_words[: min(16, len(following_words))])
    caption_start = " ".join(caption_words[: min(16, len(caption_words))])
    return difflib.SequenceMatcher(None, caption_start, following_start).ratio() >= 0.62



def previous_nonempty_output_index(lines: list[str]) -> int | None:
    """Return the last non-empty index from an output line buffer."""
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip():
            return index
    return None


def next_nonempty_source_index(lines: list[str], start_index: int) -> int | None:
    """Return the next non-empty source-line index, if any."""
    for index in range(start_index, len(lines)):
        if lines[index].strip():
            return index
    return None


def collect_line_block(lines: list[str], start_index: int) -> tuple[list[str], int, bool]:
    """Collect one visual/text block from start_index until a blank line.

    Returns the collected non-empty lines, the next index after the block and
    following blank lines, and whether at least one blank line was consumed.
    """
    block: list[str] = []
    index = start_index

    while index < len(lines) and lines[index].strip():
        block.append(lines[index].strip())
        index += 1

    consumed_blank = False
    while index < len(lines) and not lines[index].strip():
        consumed_blank = True
        index += 1

    return block, index, consumed_blank


def relocate_side_note_caption_lines(text: str) -> tuple[str, int]:
    """Line-level relocation for standalone side-caption lines.

    This catches cases where the caption is separated by only a single newline,
    so paragraph splitting on blank lines cannot see it as an independent
    paragraph.
    """
    if not text.strip():
        return text, 0

    had_final_newline = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    relocated: list[str] = []
    moved_count = 0
    index = 0

    while index < len(lines):
        current_line = lines[index].strip()
        if not paragraph_looks_like_side_note_caption(current_line):
            relocated.append(lines[index])
            index += 1
            continue

        next_index = next_nonempty_source_index(lines, index + 1)
        if next_index is None:
            relocated.append(lines[index])
            index += 1
            continue

        next_block, after_next_block, consumed_blank = collect_line_block(
            lines,
            next_index,
        )
        if not next_block:
            relocated.append(lines[index])
            index += 1
            continue

        previous_index = previous_nonempty_output_index(relocated)
        previous_text = relocated[previous_index].strip() if previous_index is not None else ""
        next_block_text = " ".join(next_block)

        # Shape: previous fragment -> side caption -> continuation block.
        #
        # Older versions appended the caption after the entire next visual block.
        # In illustrated PDFs one extracted block may contain several real
        # paragraphs and even another side caption, so this made captions drift
        # too far down the text. Consume only the continuation lines needed to
        # complete the broken paragraph, then resume scanning the source lines so
        # later captions can be handled independently.
        if (
            previous_index is not None
            and previous_text
            and not ends_at_safe_boundary(previous_text)
            and paragraph_starts_like_continuation(next_block[0])
        ):
            target_lines: list[str] = []
            target_index = next_index

            while target_index < len(lines) and lines[target_index].strip():
                target_line = lines[target_index].strip()
                target_lines.append(target_line)
                target_index += 1

                if ends_at_safe_boundary(target_line):
                    break

            if not target_lines:
                relocated.append(lines[index])
                index += 1
                continue

            relocated[previous_index] = join_split_paragraph_fragments(
                relocated[previous_index],
                target_lines[0],
            )
            relocated.extend(target_lines[1:])
            relocated.append(current_line)
            moved_count += 1
            index = target_index
            continue

        # Shape: side caption -> paragraph it summarizes. Move the caption
        # after the first completed following paragraph, not after every
        # subsequent non-empty line in the same extracted block.
        if side_note_caption_matches_following_paragraph(
            current_line,
            next_block_text,
        ):
            target_lines: list[str] = []
            remaining_lines: list[str] = []
            target_is_complete = False

            for block_line in next_block:
                if target_is_complete:
                    remaining_lines.append(block_line)
                    continue

                target_lines.append(block_line)
                if ends_at_safe_boundary(block_line):
                    target_is_complete = True

            relocated.extend(target_lines)
            relocated.append(current_line)
            relocated.extend(remaining_lines)
            moved_count += 1
            index = after_next_block
            if consumed_blank and index < len(lines):
                relocated.append("")
            continue

        relocated.append(lines[index])
        index += 1

    if not moved_count:
        return text, 0

    relocated_text = "\n".join(relocated)
    if had_final_newline:
        relocated_text += "\n"

    return relocated_text, moved_count


def relocate_interrupting_side_note_captions(text: str) -> tuple[str, int]:
    """Move side captions after the paragraph they interrupt.

    OCR/layout extraction can emit a marginal caption between a paragraph that
    starts at the bottom of one page and continues at the top of the next page:

        ... both the feel of the
        BIRTH CONTROL opposite ...
        vagina and the genital odor ...

    The caption is real text and should not be deleted. This pass moves it after
    the completed adjacent paragraph, allowing page-boundary paragraph joining to
    read naturally while preserving the caption.
    """
    if not text.strip():
        return text, 0

    text, line_moved_count = relocate_side_note_caption_lines(text)

    had_final_newline = text.endswith(("\n", "\r"))
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n+", text)
        if paragraph.strip()
    ]

    if len(paragraphs) < 3:
        return text, line_moved_count

    relocated: list[str] = []
    moved_count = 0
    index = 0

    while index < len(paragraphs):
        # Shape from embedded text layers that emit a side caption immediately
        # before the main paragraph it summarizes. Move it after that paragraph
        # when the caption text clearly overlaps the paragraph start.
        if (
            index + 1 < len(paragraphs)
            and paragraph_looks_like_side_note_caption(paragraphs[index])
            and not paragraph_looks_like_side_note_caption(paragraphs[index + 1])
            and side_note_caption_matches_following_paragraph(
                paragraphs[index],
                paragraphs[index + 1],
            )
        ):
            relocated.append(paragraphs[index + 1])
            relocated.append(paragraphs[index])
            moved_count += 1
            index += 2
            continue

        # Shape after OCR block ordering:
        #   main fragment -> side caption -> continuation fragment
        if (
            index + 2 < len(paragraphs)
            and not ends_at_safe_boundary(paragraphs[index])
            and paragraph_looks_like_side_note_caption(paragraphs[index + 1])
            and paragraph_starts_like_continuation(paragraphs[index + 2])
        ):
            relocated.append(
                join_split_paragraph_fragments(paragraphs[index], paragraphs[index + 2])
            )
            relocated.append(paragraphs[index + 1])
            moved_count += 1
            index += 3
            continue

        # Shape from some embedded text layers:
        #   side caption -> main fragment -> continuation fragment
        if (
            index + 2 < len(paragraphs)
            and paragraph_looks_like_side_note_caption(paragraphs[index])
            and not ends_at_safe_boundary(paragraphs[index + 1])
            and paragraph_starts_like_continuation(paragraphs[index + 2])
        ):
            relocated.append(
                join_split_paragraph_fragments(paragraphs[index + 1], paragraphs[index + 2])
            )
            relocated.append(paragraphs[index])
            moved_count += 1
            index += 3
            continue

        relocated.append(paragraphs[index])
        index += 1

    if not moved_count:
        return text, line_moved_count

    relocated_text = "\n\n".join(relocated)
    if had_final_newline:
        relocated_text += "\n"

    return relocated_text, moved_count + line_moved_count


def clean_txt_content(
    text: str,
    custom_clean_string: str = "",
    running_header_title: str = "",
    extra_repeated_numbered_header_titles: set[str] | None = None,
) -> tuple[str, int, int]:
    """Remove generated metadata/footer noise without erasing real paragraphs.

    custom_clean_string is treated as a literal plain-text value, not a regex.
    Single-line values are removed as standalone lines and inline occurrences.
    Multi-line values are removed as exact literal blocks before line cleanup.

    running_header_title is also treated as literal text. When supplied, the
    cleaner removes only line-start title + page-number prefixes, e.g.
    "The Hidden Evil  8", while preserving the paragraph text that follows.

    Generated page markers are handled contextually:
    - a standalone printed page number immediately before "### Page ..." is removed
    - a trailing printed page number at the end of the previous line is removed
    - a leading printed page number fused to the next page's first line is removed
    - a running header immediately after "### Page ..." is removed, e.g.
      "230. The Joy of Sex", "51 Starters", or "Starters 55-"
    - if the marker split one running sentence, the next line is joined back
    """
    text, removed_custom_blocks = remove_multiline_custom_clean_string(
        text,
        custom_clean_string,
    )

    lines = text.splitlines(keepends=True)
    repeated_numbered_header_titles = collect_numbered_running_header_titles(lines)
    if extra_repeated_numbered_header_titles:
        repeated_numbered_header_titles = (
            repeated_numbered_header_titles | extra_repeated_numbered_header_titles
        )
    cleaned_lines: list[str] = []
    removed_marker_lines = removed_custom_blocks
    removed_empty_lines = 0
    index = 0
    join_next_line_to_previous = False

    while index < len(lines):
        line = lines[index]

        if not cleaned_lines and line_is_initial_running_header(
            lines,
            index,
            repeated_numbered_header_titles,
        ):
            removed_marker_lines += 1
            index += 1

            if index < len(lines) and not lines[index].strip():
                removed_empty_lines += 1
                index += 1

            continue

        if not cleaned_lines:
            stripped_line, removed_initial_header_prefix = (
                strip_contextual_numbered_running_header_prefix(
                    line,
                    repeated_numbered_header_titles,
                )
            )
            if removed_initial_header_prefix:
                removed_marker_lines += 1
                if not stripped_line:
                    index += 1

                    if index < len(lines) and not lines[index].strip():
                        removed_empty_lines += 1
                        index += 1

                    continue

                line = stripped_line
                lines[index] = stripped_line

        if line_is_generated_page_marker(line):
            removed_marker_lines += 1

            # Remove the blank line directly before the generated page marker.
            # In PDF chunks this blank line usually separates the marker from a
            # printed page number or from a paragraph split across pages.
            while cleaned_lines and not cleaned_lines[-1].strip():
                cleaned_lines.pop()
                removed_empty_lines += 1

            # Remove a printed page number on its own line immediately before
            # the generated page marker, e.g. "... body.\n\n44\n\n### Page 11".
            if cleaned_lines and line_is_standalone_page_number(cleaned_lines[-1]):
                cleaned_lines.pop()
                removed_marker_lines += 1

            # Remove a printed page number appended to the previous text line,
            # e.g. "... brought in tea 35\n\n### Page 2".
            if cleaned_lines:
                cleaned_line, removed_inline_page_number = remove_trailing_page_number(
                    cleaned_lines[-1]
                )
                if removed_inline_page_number:
                    cleaned_lines[-1] = cleaned_line
                    removed_marker_lines += 1

            index += 1

            while index < len(lines) and not lines[index].strip():
                removed_empty_lines += 1
                index += 1

            (
                index,
                removed_numbered_header_lines,
                removed_numbered_header_empty_lines,
            ) = remove_contextual_numbered_running_header(
                lines,
                index,
                repeated_numbered_header_titles,
            )
            removed_marker_lines += removed_numbered_header_lines
            removed_empty_lines += removed_numbered_header_empty_lines

            previous_text = last_nonempty_line_text(cleaned_lines)

            if index < len(lines):
                stripped_line, removed_header_prefix = (
                    strip_contextual_numbered_running_header_prefix(
                        lines[index],
                        repeated_numbered_header_titles,
                    )
                )
                if removed_header_prefix:
                    lines[index] = stripped_line
                    removed_marker_lines += 1

            if index < len(lines):
                stripped_line, removed_leading_page_number = (
                    remove_leading_page_number_after_marker(
                        lines[index],
                        previous_text,
                    )
                )
                if removed_leading_page_number:
                    lines[index] = stripped_line
                    removed_marker_lines += 1

            next_text = next_nonempty_line_text(lines, index)
            join_next_line_to_previous = should_join_page_boundary(
                previous_text,
                next_text,
            )

            if (
                previous_text
                and next_text
                and not join_next_line_to_previous
                and cleaned_lines
                and cleaned_lines[-1].strip()
            ):
                cleaned_lines.append("\n")

            continue

        if line_is_standalone_clean_marker(line) or line_is_standalone_custom_clean_string(
            line,
            custom_clean_string,
        ):
            removed_marker_lines += 1
            index += 1

            if index < len(lines) and not lines[index].strip():
                removed_empty_lines += 1
                index += 1

            continue

        line, removed_running_header = strip_running_header_prefix(
            line,
            running_header_title=running_header_title,
        )
        if removed_running_header:
            removed_marker_lines += 1

            if not line:
                index += 1

                if index < len(lines) and not lines[index].strip():
                    removed_empty_lines += 1
                    index += 1

                continue

        line, removed_learned_running_header = strip_learned_running_header_prefix(
            line,
            repeated_numbered_header_titles,
        )
        if removed_learned_running_header:
            removed_marker_lines += 1

            if not line:
                index += 1

                if index < len(lines) and not lines[index].strip():
                    removed_empty_lines += 1
                    index += 1

                continue

        cleaned_line, changed = strip_embedded_clean_noise(
            line,
            custom_clean_string=custom_clean_string,
        )
        if changed:
            removed_marker_lines += 1

        if cleaned_line:
            append_cleaned_line(
                cleaned_lines,
                cleaned_line,
                join_with_previous=join_next_line_to_previous,
            )
            join_next_line_to_previous = False

        index += 1

    terminal_removed_marker_lines, terminal_removed_empty_lines = (
        remove_terminal_page_number_footer(cleaned_lines)
    )
    removed_marker_lines += terminal_removed_marker_lines
    removed_empty_lines += terminal_removed_empty_lines

    cleaned_text = "".join(cleaned_lines)
    cleaned_text, _ = relocate_interrupting_side_note_captions(cleaned_text)

    return cleaned_text, removed_marker_lines, removed_empty_lines


def collect_numbered_running_header_titles_from_files(
    txt_files: list[Path],
) -> set[str]:
    """Learn repeated running-header titles across a folder of TXT pages.

    This catches cleaned single-page files where page markers are already gone,
    so each file starts directly with a header such as "The Joy of Sex 114•".
    """
    title_counts: dict[str, int] = {}

    def add_candidate(line: str) -> None:
        # Try prefix parsing first. Otherwise an inline-merged line such as
        # "151 Main Courses initially..." can be misread as one giant
        # standalone title.
        prefix_parts = numbered_running_header_prefix_parts(line)
        if prefix_parts is not None:
            _, _, normalized_title, _ = prefix_parts
            title_counts[normalized_title] = title_counts.get(normalized_title, 0) + 1
            return

        parts = numbered_running_header_parts(line)
        if parts is None:
            return

        _, _, normalized_title = parts
        title_counts[normalized_title] = title_counts.get(normalized_title, 0) + 1

    for txt_path in txt_files:
        try:
            text = txt_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                text = txt_path.read_text(encoding="utf-8-sig")
            except Exception:
                continue
        except Exception:
            continue

        lines = text.splitlines()

        for line in lines:
            if line.strip():
                add_candidate(line)
                break

        index = 0
        while index < len(lines):
            if not line_is_generated_page_marker(lines[index]):
                index += 1
                continue

            index += 1
            while index < len(lines) and not lines[index].strip():
                index += 1

            if index < len(lines):
                add_candidate(lines[index])

    return {title for title, count in title_counts.items() if count >= 2}


def clean_txt_files_in_folder(
    folder: Path,
    custom_clean_string: str = "",
    running_header_title: str = "",
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> TextCleanResult:
    """Clean all TXT files directly inside a selected folder."""
    txt_files = find_txt_files(folder)

    if not txt_files:
        raise TextCleanerError("No TXT files were found in the selected folder.")

    folder_repeated_numbered_header_titles = (
        collect_numbered_running_header_titles_from_files(txt_files)
    )

    cleaned_files: list[Path] = []
    errors: list[str] = []
    total_removed_marker_lines = 0
    total_removed_empty_lines = 0

    for index, txt_path in enumerate(txt_files, start=1):
        if progress_callback:
            progress_callback(f"Cleaning TXT: {txt_path.name}", index - 1, len(txt_files))

        try:
            original_text = txt_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                original_text = txt_path.read_text(encoding="utf-8-sig")
            except Exception as exc:
                errors.append(f"{txt_path.name}: skipped: could not read as UTF-8: {exc}")
                continue
        except Exception as exc:
            errors.append(f"{txt_path.name}: skipped: {exc}")
            continue

        cleaned_text, removed_marker_lines, removed_empty_lines = clean_txt_content(
            original_text,
            custom_clean_string=custom_clean_string,
            running_header_title=running_header_title,
            extra_repeated_numbered_header_titles=folder_repeated_numbered_header_titles,
        )

        total_removed_marker_lines += removed_marker_lines
        total_removed_empty_lines += removed_empty_lines

        if cleaned_text == original_text:
            continue

        try:
            txt_path.write_text(cleaned_text, encoding="utf-8")
            cleaned_files.append(txt_path)
        except Exception as exc:
            errors.append(f"{txt_path.name}: skipped: could not write cleaned text: {exc}")

    if progress_callback:
        progress_callback("Done.", len(txt_files), len(txt_files))

    return TextCleanResult(
        cleaned_files=cleaned_files,
        scanned_files=len(txt_files),
        changed_files=len(cleaned_files),
        removed_marker_lines=total_removed_marker_lines,
        removed_empty_lines=total_removed_empty_lines,
        errors=errors,
    )


def token_counter() -> Callable[[str], int]:
    """Return a token-counting function."""
    import tiktoken

    try:
        encoding = tiktoken.get_encoding("o200k_base")
    except Exception:
        encoding = tiktoken.get_encoding("cl100k_base")

    return lambda text: len(encoding.encode(text))


def recognize_text_from_image(image_path: Path) -> str:
    """Extract text from one image using Apple Vision OCR."""
    import Vision
    from Foundation import NSURL

    url = NSURL.fileURLWithPath_(str(image_path.resolve()))
    collected_lines: list[OCRLine] = []
    completion_errors: list[str] = []

    def completion_handler(request, error) -> None:
        if error is not None:
            completion_errors.append(str(error))
            return

        observations = request.results() or []

        for observation in observations:
            candidates = observation.topCandidates_(1)
            if not candidates:
                continue

            text = str(candidates[0].string()).strip()
            if not text:
                continue

            box = observation.boundingBox()
            collected_lines.append(
                OCRLine(
                    text=text,
                    x=float(box.origin.x),
                    top=float(box.origin.y + box.size.height),
                    bottom=float(box.origin.y),
                    height=float(box.size.height),
                )
            )

    request = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(
        completion_handler
    )
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)

    if hasattr(request, "setAutomaticallyDetectsLanguage_"):
        request.setAutomaticallyDetectsLanguage_(True)

    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, {})
    success, error = handler.performRequests_error_([request], None)

    if not success:
        raise OCRChunkerError(f"OCR failed for {image_path.name}: {error}")

    if completion_errors:
        raise OCRChunkerError(f"OCR failed for {image_path.name}: {completion_errors[0]}")

    return lines_to_paragraphs(collected_lines)


def normalize_extracted_line_text(text: str) -> str:
    """Normalize spacing inside one extracted OCR/PDF line."""
    text = re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip()
    text = re.sub(r"\s+([,.;:!?%)\]}>])", r"\1", text)
    text = re.sub(r"([([{<])\s+", r"\1", text)
    return text


def median_float(values: list[float], fallback: float = 0.0) -> float:
    """Return the median of a list without importing statistics."""
    if not values:
        return fallback

    ordered = sorted(values)
    middle = len(ordered) // 2

    if len(ordered) % 2:
        return ordered[middle]

    return (ordered[middle - 1] + ordered[middle]) / 2


def estimate_main_text_left(lines: list[OCRLine]) -> float | None:
    """Estimate the dominant main-body left edge from OCR line positions.

    Marginal labels are usually short and less frequent. Long lines are a much
    better signal for the main text column, so they get priority. This prevents
    left-side labels such as "birdsong / at morning" from being treated as the
    start of the paragraph.
    """
    long_line_x_values = [
        line.x
        for line in lines
        if len(normalize_extracted_line_text(line.text)) >= LAYOUT_MARGIN_MIN_MAIN_TEXT_CHARS
    ]
    if long_line_x_values:
        return median_float(long_line_x_values)

    text_x_values = [line.x for line in lines if normalize_extracted_line_text(line.text)]
    if not text_x_values:
        return None

    return median_float(text_x_values)


def group_zone_lines(
    zone: str,
    zone_lines: list[OCRLine],
    gap_threshold: float,
) -> list[tuple[str, float, float, float, list[str]]]:
    """Group lines from one layout zone into vertical text blocks."""
    ordered = sorted(zone_lines, key=lambda line: (-line.top, line.x))
    blocks: list[tuple[str, float, float, float, list[str]]] = []
    current_lines: list[str] = []
    current_top = 0.0
    current_bottom = 0.0
    current_x = 0.0
    previous_line: OCRLine | None = None

    def flush_current() -> None:
        nonlocal current_lines, current_top, current_bottom, current_x
        if current_lines:
            blocks.append((zone, current_top, current_bottom, current_x, current_lines))
        current_lines = []
        current_top = 0.0
        current_bottom = 0.0
        current_x = 0.0

    for line in ordered:
        cleaned_text = normalize_extracted_line_text(line.text)
        if not cleaned_text:
            continue

        if previous_line is not None:
            vertical_gap = previous_line.bottom - line.top
            if vertical_gap > gap_threshold:
                flush_current()

        if not current_lines:
            current_top = line.top
            current_x = line.x

        current_lines.append(cleaned_text)
        current_bottom = line.bottom
        current_x = min(current_x, line.x)
        previous_line = line

    flush_current()
    return blocks


def group_ocr_lines_into_blocks(
    lines: list[OCRLine],
    paragraph_gap_threshold: float,
    margin_gap_threshold: float,
) -> list[tuple[str, float, float, float, list[str]]]:
    """Group OCR lines into independent main/margin layout blocks.

    Lines are classified first, then grouped per zone. Grouping per zone is
    important: otherwise a row like "margin label | main paragraph" would split
    a two-line marginal label into separate blocks.
    """
    main_left = estimate_main_text_left(lines)
    if main_left is None:
        return []

    margin_cutoff = main_left - LAYOUT_MARGIN_X_GAP
    main_lines: list[OCRLine] = []
    margin_lines: list[OCRLine] = []

    for line in lines:
        if not normalize_extracted_line_text(line.text):
            continue

        if line.x < margin_cutoff:
            margin_lines.append(line)
        else:
            main_lines.append(line)

    blocks = [
        *group_zone_lines("main", main_lines, paragraph_gap_threshold),
        *group_zone_lines("margin", margin_lines, margin_gap_threshold),
    ]

    def block_sort_key(block: tuple[str, float, float, float, list[str]]) -> tuple[float, int, float]:
        zone, top, _, x, _ = block
        # For equal/near-equal vertical starts, marginal headings should precede
        # the body paragraph they label. Otherwise normal top-to-bottom order is
        # preserved.
        zone_order = 0 if zone == "margin" else 1
        return (-round(top, 3), zone_order, x)

    return sorted(blocks, key=block_sort_key)


def merge_text_lines_for_block(text_lines: list[str]) -> str:
    """Merge visual lines from one layout block into readable text."""
    return normalize_extracted_line_text(" ".join(text_lines))


def lines_to_paragraphs(lines: list[OCRLine]) -> str:
    """Group OCR lines into paragraphs without mixing marginalia into body text.

    Apple Vision returns coordinates for each recognized line. The old
    implementation sorted by row and then x position, so a left-side note on
    the same row as a right-side paragraph became part of the sentence, e.g.
    "birdsong What your partner says ...".

    This version estimates the dominant main-text left edge, classifies lines
    to the left as margin/caption text, and emits those as separate blocks. It
    still preserves their page order, but it no longer joins them into unrelated
    main text.
    """
    if not lines:
        return ""

    usable_heights = [line.height for line in lines if line.height > 0]
    median_height = median_float(usable_heights, fallback=0.025)
    paragraph_gap_threshold = max(
        median_height * LAYOUT_BLOCK_VERTICAL_GAP_FACTOR,
        0.025,
    )
    margin_gap_threshold = max(
        median_height * LAYOUT_MARGIN_VERTICAL_GAP_FACTOR,
        paragraph_gap_threshold,
    )

    blocks = group_ocr_lines_into_blocks(
        lines=lines,
        paragraph_gap_threshold=paragraph_gap_threshold,
        margin_gap_threshold=margin_gap_threshold,
    )

    if not blocks:
        return ""

    paragraphs = [
        merge_text_lines_for_block(block_lines)
        for _, _, _, _, block_lines in blocks
    ]
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph).strip()


def split_into_paragraphs(text: str) -> list[str]:
    """Split text into non-empty paragraphs."""
    return [part.strip() for part in re.split(r"\n\s*\n+", text) if part.strip()]


def ends_at_safe_boundary(text: str) -> bool:
    """Check whether text ends at a sentence-like boundary."""
    return bool(SENTENCE_END_RE.search(text.strip()))


def make_safe_blocks(paragraphs: Iterable[str]) -> list[str]:
    """
    Build blocks that prefer ending after sentence-final punctuation.

    This helps avoid chunk cuts in the middle of a sentence.
    """
    blocks: list[str] = []
    buffer: list[str] = []

    for paragraph in paragraphs:
        buffer.append(paragraph)

        if ends_at_safe_boundary(paragraph):
            blocks.append("\n\n".join(buffer))
            buffer = []

    if buffer:
        if blocks:
            blocks[-1] = blocks[-1] + "\n\n" + "\n\n".join(buffer)
        else:
            blocks.append("\n\n".join(buffer))

    return blocks


def split_large_block(
    block: str,
    token_budget: int,
    count_tokens: Callable[[str], int],
) -> list[str]:
    """Split a too-large block by sentences, then words as fallback."""
    sentences = [
        sentence.strip()
        for sentence in SENTENCE_SPLIT_RE.split(block)
        if sentence.strip()
    ]

    chunks: list[str] = []
    current = ""

    for sentence in sentences:
        if count_tokens(sentence) > token_budget:
            if current:
                chunks.append(current.strip())
                current = ""

            chunks.extend(force_split_by_words(sentence, token_budget, count_tokens))
            continue

        candidate = f"{current} {sentence}".strip() if current else sentence

        if count_tokens(candidate) <= token_budget:
            current = candidate
        else:
            if current:
                chunks.append(current.strip())
            current = sentence

    if current:
        chunks.append(current.strip())

    return chunks


def force_split_by_words(
    text: str,
    token_budget: int,
    count_tokens: Callable[[str], int],
) -> list[str]:
    """
    Last-resort splitter.

    This is only used when one sentence is larger than the whole token budget.
    """
    words = text.split()
    chunks: list[str] = []
    current_words: list[str] = []

    for word in words:
        candidate_words = [*current_words, word]
        candidate = " ".join(candidate_words)

        if count_tokens(candidate) <= token_budget:
            current_words = candidate_words
            continue

        if current_words:
            chunks.append(" ".join(current_words))
            current_words = [word]
        else:
            chunks.append(word)
            current_words = []

    if current_words:
        chunks.append(" ".join(current_words))

    return chunks


def chunk_text(
    text: str,
    token_budget: int,
    count_tokens: Callable[[str], int],
) -> list[str]:
    """Split full OCR text into token-safe chunks."""
    paragraphs = split_into_paragraphs(text)
    blocks = make_safe_blocks(paragraphs)

    chunks: list[str] = []
    current = ""

    for block in blocks:
        if count_tokens(block) > token_budget:
            if current:
                chunks.append(current.strip())
                current = ""

            chunks.extend(split_large_block(block, token_budget, count_tokens))
            continue

        candidate = f"{current}\n\n{block}".strip() if current else block

        if count_tokens(candidate) <= token_budget:
            current = candidate
        else:
            if current:
                chunks.append(current.strip())
            current = block

    if current:
        chunks.append(current.strip())

    for index, chunk in enumerate(chunks, start=1):
        token_count = count_tokens(chunk)
        if token_count > token_budget:
            raise OCRChunkerError(
                f"Chunk {index} has {token_count} tokens, above budget {token_budget}."
            )

    return chunks


def remove_old_outputs(folder: Path) -> None:
    """Remove previous generated OCR files from the selected folder."""
    for old_file in folder.glob(f"{OUTPUT_PREFIX}_*.txt"):
        old_file.unlink()

    for filename in (ERRORS_FILENAME, MANIFEST_FILENAME):
        old_file = folder / filename
        if old_file.exists():
            old_file.unlink()


def write_outputs(
    folder: Path,
    chunks: list[str],
    errors: list[str],
    processed_images: int,
    skipped_images: int,
    token_budget: int,
    count_tokens: Callable[[str], int],
) -> list[Path]:
    """Write chunk files, error log, and manifest."""
    remove_old_outputs(folder)

    output_files: list[Path] = []

    for index, chunk in enumerate(chunks, start=1):
        output_path = folder / f"{OUTPUT_PREFIX}_{index:03d}.txt"
        output_path.write_text(chunk.strip() + "\n", encoding="utf-8")
        output_files.append(output_path)

    if errors:
        (folder / ERRORS_FILENAME).write_text("\n".join(errors) + "\n", encoding="utf-8")

    manifest = {
        "processed_images": processed_images,
        "skipped_images": skipped_images,
        "chunk_count": len(output_files),
        "token_budget_per_chunk": token_budget,
        "chunks": [
            {
                "file": output_file.name,
                "tokens": count_tokens(output_file.read_text(encoding="utf-8")),
            }
            for output_file in output_files
        ],
        "errors_file": ERRORS_FILENAME if errors else None,
    }

    (folder / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return output_files


def process_folder(
    folder: Path,
    model_token_capacity: int,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> ProcessingResult:
    """OCR all supported images in a folder and write text chunks."""
    images = find_images(folder)

    if not images:
        raise OCRChunkerError("No supported image files were found in the selected folder.")

    count_tokens = token_counter()
    token_budget = model_token_capacity // 2

    combined_parts: list[str] = []
    errors: list[str] = []

    for index, image_path in enumerate(images, start=1):
        if progress_callback:
            progress_callback(f"OCR: {image_path.name}", index - 1, len(images))

        try:
            image_text = recognize_text_from_image(image_path)
        except Exception as exc:
            errors.append(f"{image_path.name}: {exc}")
            continue

        if not image_text:
            errors.append(f"{image_path.name}: no text detected")
            continue

        combined_parts.append(f"### Source image: {image_path.name}\n\n{image_text}")

    if progress_callback:
        progress_callback("Chunking text...", len(images), len(images))

    if not combined_parts:
        raise OCRChunkerError("OCR finished, but no readable text was detected.")

    combined_text = "\n\n".join(combined_parts)
    chunks = chunk_text(combined_text, token_budget, count_tokens)

    output_files = write_outputs(
        folder=folder,
        chunks=chunks,
        errors=errors,
        processed_images=len(images) - len(errors),
        skipped_images=len(errors),
        token_budget=token_budget,
        count_tokens=count_tokens,
    )

    return ProcessingResult(
        output_files=output_files,
        processed_images=len(images) - len(errors),
        skipped_images=len(errors),
        token_budget=token_budget,
        errors=errors,
    )


def has_useful_text(text: str) -> bool:
    """Return True only when extracted PDF text looks genuinely readable.

    Some PDFs contain a broken embedded text layer. In those files,
    page.extract_text() can return many characters, but they are mostly glyph
    codes/control characters instead of real Unicode text. The old check only
    counted alphanumeric characters, so it could incorrectly accept corrupted
    text and skip OCR. This stricter check rejects that garbage and allows the
    page to fall back to Apple Vision OCR.
    """
    if not text or not text.strip():
        return False

    text = text.replace("\u00a0", " ")
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 20:
        return False

    def is_bad_control(character: str) -> bool:
        if character in "\n\r\t":
            return False
        category = unicodedata.category(character)
        return category.startswith("C") and character not in {"\u200c", "\u200d"}

    bad_control_count = sum(is_bad_control(character) for character in text)
    bad_control_ratio = bad_control_count / max(1, len(text))
    if bad_control_ratio > 0.02:
        return False

    printable_count = sum(
        character.isprintable() or character.isspace()
        for character in text
    )
    printable_ratio = printable_count / max(1, len(text))
    if printable_ratio < 0.90:
        return False

    letter_count = sum(character.isalpha() for character in compact)
    if letter_count < 10:
        return False

    # Works for Latin and most Unicode alphabetic scripts. This prevents random
    # punctuation/digit-heavy glyph dumps from being treated as readable prose.
    letter_ratio = letter_count / max(1, len(compact))
    if letter_ratio < 0.25:
        return False

    return True


def pdf_page_has_large_raster_image(
    fitz_page,
    min_area_ratio: float = PDF_LARGE_IMAGE_AREA_RATIO,
) -> bool:
    """Return True when a page is mainly a raster image.

    This catches scanned/searchable PDFs where the visible page is an image and
    the selectable text is only a hidden OCR layer.
    """
    try:
        page_area = float(fitz_page.rect.width * fitz_page.rect.height)
    except Exception:
        return False

    if page_area <= 0:
        return False

    try:
        image_infos = fitz_page.get_image_info(xrefs=True)
    except Exception:
        return False

    for image_info in image_infos:
        bbox = image_info.get("bbox")
        if not bbox or len(bbox) != 4:
            continue

        x0, y0, x1, y1 = (float(value) for value in bbox)
        image_area = max(0.0, x1 - x0) * max(0.0, y1 - y0)

        if image_area / page_area >= min_area_ratio:
            return True

    return False


def pdf_page_has_hidden_or_synthetic_text_layer(
    fitz_page,
    suspicious_ratio: float = PDF_SUSPICIOUS_TEXT_LAYER_RATIO,
) -> bool:
    """Return True for invisible/synthetic OCR layers such as GlyphLessFont.

    Good born-digital PDFs usually have visible fonts. Searchable scanned PDFs
    commonly have invisible OCR text (alpha=0 or render-mode hidden) over a
    page image. In the attached example, that hidden layer is corrupted and
    causes missing/reordered text unless we force OCR from the rendered image.
    """
    try:
        page_dict = fitz_page.get_text("dict")
    except Exception:
        return False

    spans: list[dict[str, object]] = []
    for block in page_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if str(span.get("text", "")).strip():
                    spans.append(span)

    if not spans:
        return False

    invisible_spans = 0
    synthetic_font_spans = 0

    for span in spans:
        alpha = span.get("alpha")
        try:
            if alpha is not None and float(alpha) <= 0.01:
                invisible_spans += 1
        except (TypeError, ValueError):
            pass

        font_name = str(span.get("font", "")).lower()
        if "glyphless" in font_name or "ocr" in font_name:
            synthetic_font_spans += 1

    span_count = len(spans)
    return (
        invisible_spans / span_count >= suspicious_ratio
        or synthetic_font_spans / span_count >= suspicious_ratio
    )


def should_ocr_pdf_page(fitz_page, direct_text: str) -> bool:
    """Decide whether to ignore direct PDF text and OCR the rendered page.

    Direct extraction is fastest and best for born-digital PDFs. OCR is safer
    for scanned/searchable PDFs with a full-page raster image and a hidden OCR
    layer, because that layer may be stale, incomplete, or badly ordered.
    """
    if not has_useful_text(direct_text):
        return True

    return (
        pdf_page_has_large_raster_image(fitz_page)
        and pdf_page_has_hidden_or_synthetic_text_layer(fitz_page)
    )


def normalize_pdf_text(text: str) -> str:
    """
    Convert direct PDF text extraction into cleaner paragraph-like blocks.

    Many PDFs expose one visual line per newline. This joins nearby lines and
    prefers paragraph breaks after sentence-ending punctuation.
    """
    raw_lines = [line.strip() for line in text.replace("\r", "\n").split("\n")]

    paragraphs: list[str] = []
    current: list[str] = []

    for line in raw_lines:
        if not line:
            if current:
                paragraphs.append(" ".join(current).strip())
                current = []
            continue

        current.append(line)

        if ends_at_safe_boundary(line):
            paragraphs.append(" ".join(current).strip())
            current = []

    if current:
        paragraphs.append(" ".join(current).strip())

    cleaned = [paragraph for paragraph in paragraphs if paragraph]
    return "\n\n".join(cleaned).strip()



def pdf_text_block_text(block: tuple[object, ...]) -> str:
    """Return normalized raw text from one PyMuPDF text block."""
    try:
        raw_text = str(block[4])
    except Exception:
        return ""

    lines = [normalize_extracted_line_text(line) for line in raw_text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def pdf_text_block_bbox(block: tuple[object, ...]) -> tuple[float, float, float, float]:
    """Return a PyMuPDF text block bbox as floats."""
    return float(block[0]), float(block[1]), float(block[2]), float(block[3])


def pdf_text_block_is_top_running_header(
    block: tuple[object, ...],
    page_height: float,
) -> bool:
    """Return True for a small running header near the top of a PDF page."""
    text = normalize_clean_paragraph_text(pdf_text_block_text(block))
    if not text:
        return False

    _, y0, _, y1 = pdf_text_block_bbox(block)
    if y1 > max(32.0, page_height * 0.07):
        return False

    return numbered_running_header_parts(text) is not None


def estimate_pdf_main_text_left(
    blocks: list[tuple[object, ...]],
    page_width: float,
) -> float | None:
    """Estimate the dominant main text column left edge from PDF text blocks."""
    candidates: list[float] = []

    for block in blocks:
        text = normalize_clean_paragraph_text(pdf_text_block_text(block))
        if len(text) < LAYOUT_MARGIN_MIN_MAIN_TEXT_CHARS:
            continue

        x0, _, x1, _ = pdf_text_block_bbox(block)
        width = x1 - x0
        if width < page_width * 0.25:
            continue

        # Very low x values are usually marginal captions in illustrated books.
        if x0 < page_width * 0.12:
            continue

        candidates.append(x0)

    if candidates:
        return median_float(candidates)

    fallback = [
        pdf_text_block_bbox(block)[0]
        for block in blocks
        if normalize_clean_paragraph_text(pdf_text_block_text(block))
    ]
    if not fallback:
        return None

    return median_float(fallback)


def group_pdf_side_blocks(
    blocks: list[tuple[object, ...]],
    page_height: float,
) -> list[list[tuple[object, ...]]]:
    """Group nearby marginal PDF text blocks into side-note/caption groups."""
    if not blocks:
        return []

    ordered = sorted(blocks, key=lambda block: (pdf_text_block_bbox(block)[1], pdf_text_block_bbox(block)[0]))
    groups: list[list[tuple[object, ...]]] = []
    current: list[tuple[object, ...]] = []
    previous_bottom: float | None = None
    gap_threshold = max(18.0, page_height * 0.045)

    for block in ordered:
        _, y0, _, y1 = pdf_text_block_bbox(block)
        if current and previous_bottom is not None and y0 - previous_bottom > gap_threshold:
            groups.append(current)
            current = []

        current.append(block)
        previous_bottom = y1

    if current:
        groups.append(current)

    return groups


def merge_pdf_text_blocks(blocks: list[tuple[object, ...]]) -> str:
    """Merge PDF text blocks into paragraph-like text using the normalizer."""
    if not blocks:
        return ""

    ordered = sorted(blocks, key=lambda block: (pdf_text_block_bbox(block)[1], pdf_text_block_bbox(block)[0]))
    return normalize_pdf_text("\n".join(pdf_text_block_text(block) for block in ordered if pdf_text_block_text(block)))


def merge_pdf_side_group(blocks: list[tuple[object, ...]]) -> str:
    """Merge one marginal/caption group into one paragraph."""
    text = " ".join(
        normalize_clean_paragraph_text(pdf_text_block_text(block))
        for block in sorted(blocks, key=lambda block: (pdf_text_block_bbox(block)[1], pdf_text_block_bbox(block)[0]))
        if pdf_text_block_text(block)
    )
    return normalize_clean_paragraph_text(text)


def extract_layout_text_from_pdf_page(fitz_page) -> str:
    """Extract one PDF page with coordinate-aware block ordering.

    pypdf/PDF embedded text order often emits marginal image captions before the
    main body even when they are visually at the bottom/side of the page. That
    breaks cross-page paragraphs. This PyMuPDF layout pass classifies the
    dominant main column separately from side captions, emits the main body
    first, and appends true side captions at the end of the page so the TXT
    cleaner can move each caption after the exact completed paragraph it
    interrupts.
    """
    try:
        raw_blocks = [block for block in fitz_page.get_text("blocks") if len(block) >= 7 and int(block[6]) == 0]
    except Exception:
        return ""

    if not raw_blocks:
        return ""

    page_width = float(fitz_page.rect.width)
    page_height = float(fitz_page.rect.height)

    text_blocks = [
        block
        for block in raw_blocks
        if pdf_text_block_text(block)
        and not pdf_text_block_is_top_running_header(block, page_height)
    ]
    if not text_blocks:
        return ""

    main_left = estimate_pdf_main_text_left(text_blocks, page_width)
    if main_left is None:
        return merge_pdf_text_blocks(text_blocks)

    margin_cutoff = main_left - max(18.0, page_width * LAYOUT_MARGIN_X_GAP)
    main_blocks: list[tuple[object, ...]] = []
    side_blocks: list[tuple[object, ...]] = []

    for block in text_blocks:
        x0, _, x1, _ = pdf_text_block_bbox(block)
        text = normalize_clean_paragraph_text(pdf_text_block_text(block))
        width = x1 - x0

        if x0 < margin_cutoff and width < page_width * 0.45:
            side_blocks.append(block)
        else:
            main_blocks.append(block)

    side_caption_groups: list[str] = []
    non_caption_side_blocks: list[tuple[object, ...]] = []

    for group in group_pdf_side_blocks(side_blocks, page_height):
        group_text = merge_pdf_side_group(group)
        if paragraph_looks_like_side_note_caption(group_text):
            side_caption_groups.append(group_text)
        else:
            non_caption_side_blocks.extend(group)

    # Non-caption marginal text can be real section text, so keep it in visual
    # order with the body. Caption-like notes are appended after the page body.
    main_text = merge_pdf_text_blocks([*main_blocks, *non_caption_side_blocks])
    parts = [part for part in [main_text, *side_caption_groups] if part]
    return "\n\n".join(parts).strip()


def render_pdf_page_to_image(
    pdf_document,
    page_index: int,
    temp_dir: Path,
    pdf_path: Path,
    dpi: int = 300,
) -> Path:
    """Render one PDF page to a temporary PNG for Apple Vision OCR."""
    zoom = dpi / 72

    # Import inside the function so dependency installation remains centralized.
    import fitz

    page = pdf_document.load_page(page_index)
    pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    image_path = temp_dir / f"{sanitize_stem(pdf_path.stem)}_page_{page_index + 1:04d}.png"
    pixmap.save(str(image_path))
    return image_path


def extract_text_from_pdf_file(
    pdf_path: Path,
    progress_callback: Callable[[str, int, int], None] | None,
    progress_state: dict[str, int],
) -> tuple[str, int, int, int, list[str]]:
    """
    Extract text from one PDF.

    Uses direct embedded-text extraction first. If a page has no useful text,
    renders that page and OCRs it with Apple Vision.
    """
    from pypdf import PdfReader
    import fitz

    errors: list[str] = []
    page_texts: list[str] = []
    direct_text_pages = 0
    ocr_pages = 0

    try:
        reader = PdfReader(str(pdf_path))
    except Exception as exc:
        raise PDFTextExtractionError(f"Could not read {pdf_path.name}: {exc}") from exc

    if getattr(reader, "is_encrypted", False):
        try:
            decrypt_result = reader.decrypt("")
        except Exception as exc:
            raise PDFTextExtractionError(
                f"{pdf_path.name}: encrypted PDF could not be opened."
            ) from exc

        if decrypt_result == 0:
            raise PDFTextExtractionError(
                f"{pdf_path.name}: password-protected PDF. Please use an unlocked PDF."
            )

    total_pages = len(reader.pages)

    try:
        fitz_document = fitz.open(str(pdf_path))
    except Exception as exc:
        raise PDFTextExtractionError(
            f"Could not open {pdf_path.name} for scanned-page OCR: {exc}"
        ) from exc

    try:
        with tempfile.TemporaryDirectory(prefix="book_utils_pdf_ocr_") as temp_name:
            temp_dir = Path(temp_name)

            for page_index, page in enumerate(reader.pages):
                progress_state["current"] += 1

                if progress_callback:
                    progress_callback(
                        f"PDF to TXT: {pdf_path.name} page {page_index + 1}/{total_pages}",
                        progress_state["current"] - 1,
                        progress_state["total"],
                    )

                page_text = ""

                fitz_page = fitz_document.load_page(page_index)

                layout_text = ""
                try:
                    layout_text = extract_layout_text_from_pdf_page(fitz_page)
                except Exception as exc:
                    errors.append(
                        f"{pdf_path.name} page {page_index + 1}: layout text failed: {exc}"
                    )

                try:
                    pypdf_text = normalize_pdf_text(page.extract_text() or "")
                except Exception as exc:
                    pypdf_text = ""
                    errors.append(
                        f"{pdf_path.name} page {page_index + 1}: direct text failed: {exc}"
                    )

                # Prefer coordinate-aware PyMuPDF text as the embedded-text
                # fallback because its block order is better than pypdf for
                # side captions. Do NOT let useful-looking layout text bypass
                # OCR, though: searchable scanned PDFs often contain a full-page
                # image plus a hidden/synthetic OCR layer whose characters are
                # corrupted even when the text length/letter ratio looks fine
                # (for example "bGClS ^t'" t^ie" instead of "beds Still the").
                # For image-first pages with hidden/synthetic text, OCR the
                # rendered page and keep the embedded layout text only as a
                # fallback if OCR fails.
                if has_useful_text(layout_text):
                    direct_text = layout_text
                else:
                    direct_text = pypdf_text

                direct_text_is_useful = has_useful_text(direct_text)
                use_ocr = should_ocr_pdf_page(
                    fitz_page,
                    direct_text,
                )

                if direct_text_is_useful and not use_ocr:
                    page_text = direct_text
                    direct_text_pages += 1
                else:
                    try:
                        image_path = render_pdf_page_to_image(
                            fitz_document,
                            page_index,
                            temp_dir,
                            pdf_path,
                        )
                        page_text = recognize_text_from_image(image_path)
                        if page_text:
                            ocr_pages += 1
                        elif direct_text_is_useful:
                            page_text = direct_text
                            direct_text_pages += 1
                            errors.append(
                                f"{pdf_path.name} page {page_index + 1}: OCR returned no text; used embedded PDF text fallback"
                            )
                    except Exception as exc:
                        if direct_text_is_useful:
                            page_text = direct_text
                            direct_text_pages += 1
                            errors.append(
                                f"{pdf_path.name} page {page_index + 1}: OCR failed ({exc}); used embedded PDF text fallback"
                            )
                        else:
                            errors.append(
                                f"{pdf_path.name} page {page_index + 1}: OCR failed: {exc}"
                            )
                            page_text = ""

                if page_text.strip():
                    page_texts.append(
                        f"### Page {page_index + 1}\n\n{page_text.strip()}"
                    )
                else:
                    errors.append(
                        f"{pdf_path.name} page {page_index + 1}: no readable text detected"
                    )
    finally:
        fitz_document.close()

    if not page_texts:
        return "", total_pages, direct_text_pages, ocr_pages, errors

    return (
        f"### Source PDF: {pdf_path.name}\n\n" + "\n\n".join(page_texts),
        total_pages,
        direct_text_pages,
        ocr_pages,
        errors,
    )


def remove_old_pdf_text_outputs(folder: Path) -> None:
    """Remove previous generated PDF text files from the selected folder."""
    # New per-PDF output names use chatgpt_pdf_<pdf-stem>.txt or
    # chatgpt_pdf_<pdf-stem>_chunk_001.txt. Also remove the older combined
    # output style chatgpt_pdf_chunk_001.txt from previous versions.
    for pattern in (f"{PDF_TEXT_OUTPUT_PREFIX}_*.txt", "chatgpt_pdf_chunk_*.txt"):
        for old_file in folder.glob(pattern):
            old_file.unlink()

    for filename in (PDF_TEXT_ERRORS_FILENAME, PDF_TEXT_MANIFEST_FILENAME):
        old_file = folder / filename
        if old_file.exists():
            old_file.unlink()


def unique_text_output_path(
    folder: Path,
    output_name: str,
    reserved_paths: set[Path],
) -> Path:
    """Return a safe text output path without overwriting non-generated files."""
    path = folder / output_name

    if path not in reserved_paths and not path.exists():
        reserved_paths.add(path)
        return path

    for counter in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        if candidate not in reserved_paths and not candidate.exists():
            reserved_paths.add(candidate)
            return candidate

    raise PDFTextExtractionError(
        f"Could not create a unique output name for {output_name}."
    )


def write_pdf_text_outputs(
    folder: Path,
    per_pdf_chunks: list[tuple[str, list[str]]],
    errors: list[str],
    processed_pdfs: int,
    skipped_pdfs: int,
    processed_pages: int,
    direct_text_pages: int,
    ocr_pages: int,
    token_budget: int,
    count_tokens: Callable[[str], int],
    source_pdfs: list[str],
) -> list[Path]:
    """Write one separate group of text chunk files per source PDF.

    Unlike image OCR, PDF extraction must not combine multiple PDFs into one
    text chunk. Each PDF is chunked independently against the token budget.
    """
    remove_old_pdf_text_outputs(folder)

    output_files: list[Path] = []
    manifest_pdfs: list[dict[str, object]] = []
    reserved_paths: set[Path] = set()

    for pdf_name, chunks in per_pdf_chunks:
        safe_stem = sanitize_stem(Path(pdf_name).stem)
        pdf_output_entries: list[dict[str, object]] = []

        for index, chunk in enumerate(chunks, start=1):
            if len(chunks) == 1:
                output_name = f"{PDF_TEXT_OUTPUT_PREFIX}_{safe_stem}.txt"
            else:
                output_name = (
                    f"{PDF_TEXT_OUTPUT_PREFIX}_{safe_stem}_chunk_{index:03d}.txt"
                )

            output_path = unique_text_output_path(
                folder=folder,
                output_name=output_name,
                reserved_paths=reserved_paths,
            )
            output_path.write_text(chunk.strip() + "\n", encoding="utf-8")
            output_files.append(output_path)
            pdf_output_entries.append(
                {
                    "file": output_path.name,
                    "tokens": count_tokens(chunk),
                }
            )

        manifest_pdfs.append(
            {
                "source_pdf": pdf_name,
                "chunk_count": len(chunks),
                "outputs": pdf_output_entries,
            }
        )

    if errors:
        (folder / PDF_TEXT_ERRORS_FILENAME).write_text(
            "\n".join(errors) + "\n",
            encoding="utf-8",
        )

    manifest = {
        "source_pdfs_in_order": source_pdfs,
        "processed_pdfs": processed_pdfs,
        "skipped_pdfs": skipped_pdfs,
        "processed_pages": processed_pages,
        "direct_text_pages": direct_text_pages,
        "ocr_pages": ocr_pages,
        "output_file_count": len(output_files),
        "token_budget_per_chunk": token_budget,
        "pdfs": manifest_pdfs,
        "errors_file": PDF_TEXT_ERRORS_FILENAME if errors else None,
    }

    (folder / PDF_TEXT_MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return output_files


def process_pdf_folder_to_text(
    folder: Path,
    model_token_capacity: int,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> PDFTextResult:
    """
    Extract text from all PDFs in a folder and write token-safe text files.

    PDFs are processed in alphabetical/natural filename order. Each PDF is
    chunked independently, so one output .txt file never mixes text from
    multiple source PDFs.
    """
    pdfs = find_pdfs(folder)

    if not pdfs:
        raise PDFTextExtractionError("No PDF files were found in the selected folder.")

    count_tokens = token_counter()
    token_budget = model_token_capacity // 2

    # Read page counts first so the progress bar reflects all PDFs/pages.
    pdf_page_counts: dict[Path, int] = {}
    errors: list[str] = []

    for pdf_path in pdfs:
        try:
            pdf_page_counts[pdf_path] = read_pdf_total_pages(pdf_path)
        except Exception as exc:
            errors.append(f"{pdf_path.name}: skipped: {exc}")

    processable_pdfs = [pdf_path for pdf_path in pdfs if pdf_path in pdf_page_counts]

    if not processable_pdfs:
        raise PDFTextExtractionError(
            "No readable PDFs were found in the selected folder."
        )

    total_pages = sum(pdf_page_counts.values())
    progress_state = {"current": 0, "total": max(total_pages, 1)}

    per_pdf_chunks: list[tuple[str, list[str]]] = []
    source_pdfs: list[str] = []
    processed_pdfs = 0
    processed_pages = 0
    direct_text_pages = 0
    ocr_pages = 0

    for pdf_path in processable_pdfs:
        try:
            pdf_text, page_count, direct_pages, ocr_page_count, pdf_errors = extract_text_from_pdf_file(
                pdf_path=pdf_path,
                progress_callback=progress_callback,
                progress_state=progress_state,
            )
        except Exception as exc:
            errors.append(f"{pdf_path.name}: skipped: {exc}")
            continue

        errors.extend(pdf_errors)
        processed_pages += page_count
        direct_text_pages += direct_pages
        ocr_pages += ocr_page_count

        if pdf_text.strip():
            if progress_callback:
                progress_callback(
                    f"Chunking PDF text: {pdf_path.name}",
                    progress_state["current"],
                    progress_state["total"],
                )

            # Important: chunk each PDF independently so no output .txt file
            # ever contains text from two different PDFs.
            chunks = chunk_text(pdf_text, token_budget, count_tokens)
            per_pdf_chunks.append((pdf_path.name, chunks))
            source_pdfs.append(pdf_path.name)
            processed_pdfs += 1
        else:
            errors.append(f"{pdf_path.name}: skipped: no readable text detected")

    if progress_callback:
        progress_callback("Writing PDF text files...", total_pages, total_pages)

    if not per_pdf_chunks:
        raise PDFTextExtractionError(
            "PDF text extraction finished, but no readable text was detected."
        )

    output_files = write_pdf_text_outputs(
        folder=folder,
        per_pdf_chunks=per_pdf_chunks,
        errors=errors,
        processed_pdfs=processed_pdfs,
        skipped_pdfs=len(pdfs) - processed_pdfs,
        processed_pages=processed_pages,
        direct_text_pages=direct_text_pages,
        ocr_pages=ocr_pages,
        token_budget=token_budget,
        count_tokens=count_tokens,
        source_pdfs=source_pdfs,
    )

    return PDFTextResult(
        output_files=output_files,
        processed_pdfs=processed_pdfs,
        skipped_pdfs=len(pdfs) - processed_pdfs,
        processed_pages=processed_pages,
        direct_text_pages=direct_text_pages,
        ocr_pages=ocr_pages,
        token_budget=token_budget,
        errors=errors,
    )


def sanitize_stem(stem: str) -> str:
    """Make a readable, safe output filename stem."""
    cleaned = SAFE_FILENAME_RE.sub("_", stem.strip())
    cleaned = cleaned.strip("._-")
    return cleaned or "split_pdf"


def unique_output_path(path: Path) -> Path:
    """Return a non-existing path by appending a counter when needed."""
    if not path.exists():
        return path

    for counter in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        if not candidate.exists():
            return candidate

    raise PDFSplitterError(f"Could not create a unique output name for {path.name}.")


def read_pdf_total_pages(pdf_path: Path) -> int:
    """Return page count for a PDF."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(pdf_path))
    except Exception as exc:
        raise PDFSplitterError(f"Could not read PDF: {exc}") from exc

    if getattr(reader, "is_encrypted", False):
        try:
            decrypt_result = reader.decrypt("")
        except Exception as exc:
            raise PDFSplitterError(
                "The selected PDF is encrypted and could not be opened."
            ) from exc

        if decrypt_result == 0:
            raise PDFSplitterError(
                "The selected PDF is password-protected. Please use an unlocked PDF."
            )

    return len(reader.pages)


def validate_pdf_ranges(ranges: Iterable[PDFPartRange], total_pages: int) -> list[PDFPartRange]:
    """Validate 1-based inclusive page ranges."""
    validated = list(ranges)

    if not validated:
        raise PDFSplitterError("Add at least one PDF part range.")

    for index, part in enumerate(validated, start=1):
        if part.start_page < 1 or part.end_page < 1:
            raise PDFSplitterError(f"Part {index}: page numbers must be 1 or higher.")

        if part.start_page > part.end_page:
            raise PDFSplitterError(
                f"Part {index}: start page must be lower than or equal to end page."
            )

        if part.end_page > total_pages:
            raise PDFSplitterError(
                f"Part {index}: end page {part.end_page} is above the PDF's "
                f"total page count ({total_pages})."
            )

    return validated


def split_pdf(
    pdf_path: Path,
    ranges: Iterable[PDFPartRange],
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> PDFSplitResult:
    """Split one PDF into multiple 1-based inclusive page ranges."""
    from pypdf import PdfReader, PdfWriter

    if not pdf_path.exists() or not pdf_path.is_file():
        raise PDFSplitterError("Select a valid PDF file first.")

    if pdf_path.suffix.lower() != ".pdf":
        raise PDFSplitterError("The selected file must be a PDF.")

    try:
        reader = PdfReader(str(pdf_path))
    except Exception as exc:
        raise PDFSplitterError(f"Could not read PDF: {exc}") from exc

    if getattr(reader, "is_encrypted", False):
        try:
            decrypt_result = reader.decrypt("")
        except Exception as exc:
            raise PDFSplitterError(
                "The selected PDF is encrypted and could not be opened."
            ) from exc

        if decrypt_result == 0:
            raise PDFSplitterError(
                "The selected PDF is password-protected. Please use an unlocked PDF."
            )

    total_pages = len(reader.pages)
    validated_ranges = validate_pdf_ranges(ranges, total_pages)
    output_dir = pdf_path.parent
    safe_stem = sanitize_stem(pdf_path.stem)
    output_files: list[Path] = []

    for index, part in enumerate(validated_ranges, start=1):
        if progress_callback:
            progress_callback(
                f"Creating part {index}: pages {part.start_page}-{part.end_page}",
                index - 1,
                len(validated_ranges),
            )

        writer = PdfWriter()

        # pypdf uses 0-based page indexes; the UI uses normal 1-based page numbers.
        for page_number in range(part.start_page, part.end_page + 1):
            writer.add_page(reader.pages[page_number - 1])

        output_name = (
            f"{safe_stem}_"
            f"p{part.start_page:04d}-p{part.end_page:04d}.pdf"
        )
        output_path = unique_output_path(output_dir / output_name)

        try:
            with output_path.open("wb") as output_file:
                writer.write(output_file)
        except Exception as exc:
            raise PDFSplitterError(f"Could not write {output_path.name}: {exc}") from exc

        output_files.append(output_path)

    if progress_callback:
        progress_callback("Done.", len(validated_ranges), len(validated_ranges))

    return PDFSplitResult(
        output_files=output_files,
        source_pdf=pdf_path,
        total_pages=total_pages,
    )


class BookUtilsApp:
    """Tkinter GUI application."""

    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.selected_folder: Path | None = None
        self.selected_pdf_text_folder: Path | None = None
        self.selected_text_clean_folder: Path | None = None
        self.selected_pdf: Path | None = None
        self.custom_clean_text = None
        self.running_header_title_entry = None
        self.pdf_part_rows: list[tuple[object, object, object]] = []
        self.is_running = False

        self.root = tk.Tk()
        self.root.title("Book Utils: OCR + PDF Tools")
        self.root.geometry("820x700")
        self.root.minsize(780, 640)

        self.status_var = tk.StringVar(value="Choose an action.")
        self.folder_var = tk.StringVar(value="No image folder selected")
        self.pdf_text_folder_var = tk.StringVar(value="No PDF folder selected")
        self.text_clean_folder_var = tk.StringVar(value="No TXT folder selected")
        self.pdf_var = tk.StringVar(value="No PDF selected")
        self.pdf_pages_var = tk.StringVar(value="")
        self.capacity_var = tk.StringVar(value=str(DEFAULT_MODEL_TOKEN_CAPACITY))
        self.running_header_title_var = tk.StringVar(value="")

        self._build_ui()

    def _build_ui(self) -> None:
        frame = self.ttk.Frame(self.root, padding=18)
        frame.pack(fill="both", expand=True)

        title = self.ttk.Label(
            frame,
            text="Book Utils",
            font=("Helvetica", 18, "bold"),
        )
        title.pack(anchor="w", pady=(0, 4))

        description = self.ttk.Label(
            frame,
            text=(
                "Convert JPG images or PDFs to ChatGPT-safe text chunks, clean TXT "
                "metadata markers, or split a selected PDF into page ranges saved "
                "beside the original file."
            ),
            wraplength=700,
        )
        description.pack(anchor="w", pady=(0, 12))

        notebook = self.ttk.Notebook(frame)
        notebook.pack(fill="both", expand=True, pady=(0, 12))

        ocr_tab = self.ttk.Frame(notebook, padding=12)
        pdf_text_tab = self.ttk.Frame(notebook, padding=12)
        text_clean_tab = self.ttk.Frame(notebook, padding=12)
        pdf_tab = self.ttk.Frame(notebook, padding=12)
        notebook.add(ocr_tab, text="JPG to TXT")
        notebook.add(pdf_text_tab, text="PDF to TXT")
        notebook.add(text_clean_tab, text="Clean TXT")
        notebook.add(pdf_tab, text="Split PDF")

        self._build_ocr_tab(ocr_tab)
        self._build_pdf_text_tab(pdf_text_tab)
        self._build_text_clean_tab(text_clean_tab)
        self._build_pdf_tab(pdf_tab)

        self.progress_bar = self.ttk.Progressbar(
            frame,
            mode="determinate",
        )
        self.progress_bar.pack(fill="x", pady=(0, 8))

        status_label = self.ttk.Label(
            frame,
            textvariable=self.status_var,
            wraplength=700,
        )
        status_label.pack(anchor="w")

    def _build_ocr_tab(self, parent) -> None:
        description = self.ttk.Label(
            parent,
            text=(
                "Choose a folder containing images. The app extracts text and writes "
                "consecutive .txt chunks into that same folder."
            ),
            wraplength=660,
        )
        description.pack(anchor="w", pady=(0, 14))

        folder_row = self.ttk.Frame(parent)
        folder_row.pack(fill="x", pady=(0, 10))

        self.select_folder_button = self.ttk.Button(
            folder_row,
            text="Select Folder",
            command=self.select_folder,
        )
        self.select_folder_button.pack(side="left")

        folder_label = self.ttk.Label(
            folder_row,
            textvariable=self.folder_var,
            wraplength=500,
        )
        folder_label.pack(side="left", padx=(12, 0), fill="x", expand=True)

        capacity_row = self.ttk.Frame(parent)
        capacity_row.pack(fill="x", pady=(0, 12))

        capacity_label = self.ttk.Label(
            capacity_row,
            text="Model token capacity:",
        )
        capacity_label.pack(side="left")

        capacity_entry = self.ttk.Entry(
            capacity_row,
            textvariable=self.capacity_var,
            width=12,
        )
        capacity_entry.pack(side="left", padx=(8, 8))

        hint = self.ttk.Label(
            capacity_row,
            text="Each output chunk is <= half this value.",
        )
        hint.pack(side="left")

        self.convert_button = self.ttk.Button(
            parent,
            text="JPG to TXT",
            command=self.start_conversion,
        )
        self.convert_button.pack(anchor="w")

    def _build_pdf_text_tab(self, parent) -> None:
        description = self.ttk.Label(
            parent,
            text=(
                "Choose a folder containing PDFs. The app handles all PDFs in "
                "alphabetical order, extracts copyable text directly, OCRs scanned "
                "pages when needed, and writes separate .txt output files per PDF "
                "into that same folder."
            ),
            wraplength=700,
        )
        description.pack(anchor="w", pady=(0, 14))

        folder_row = self.ttk.Frame(parent)
        folder_row.pack(fill="x", pady=(0, 10))

        self.select_pdf_text_folder_button = self.ttk.Button(
            folder_row,
            text="Select Folder",
            command=self.select_pdf_text_folder,
        )
        self.select_pdf_text_folder_button.pack(side="left")

        folder_label = self.ttk.Label(
            folder_row,
            textvariable=self.pdf_text_folder_var,
            wraplength=540,
        )
        folder_label.pack(side="left", padx=(12, 0), fill="x", expand=True)

        capacity_row = self.ttk.Frame(parent)
        capacity_row.pack(fill="x", pady=(0, 12))

        capacity_label = self.ttk.Label(
            capacity_row,
            text="Model token capacity:",
        )
        capacity_label.pack(side="left")

        capacity_entry = self.ttk.Entry(
            capacity_row,
            textvariable=self.capacity_var,
            width=12,
        )
        capacity_entry.pack(side="left", padx=(8, 8))

        hint = self.ttk.Label(
            capacity_row,
            text="Each output chunk is <= half this value.",
        )
        hint.pack(side="left")

        self.pdf_text_button = self.ttk.Button(
            parent,
            text="PDF to TXT",
            command=self.start_pdf_text_extraction,
        )
        self.pdf_text_button.pack(anchor="w")

    def _build_text_clean_tab(self, parent) -> None:
        description = self.ttk.Label(
            parent,
            text=(
                "Choose a folder containing TXT files. The app removes standalone "
                "generated metadata/footer lines such as '### Source', '### Page', "
                "'Page 365 of 477', and '1 min left in chapter' including "
                "common OCR variants like 'lett in chapter', 'Learning reading "
                "speed', and page percent lines. If Kindle footer text was "
                "merged into a real paragraph, it "
                "strips only the footer text and keeps the paragraph. You can also "
                "remove title-based running headers such as 'The Hidden Evil  8', "
                "or paste a custom literal string to remove from the TXT files."
            ),
            wraplength=700,
        )
        description.pack(anchor="w", pady=(0, 14))

        folder_row = self.ttk.Frame(parent)
        folder_row.pack(fill="x", pady=(0, 10))

        self.select_text_clean_folder_button = self.ttk.Button(
            folder_row,
            text="Select Folder",
            command=self.select_text_clean_folder,
        )
        self.select_text_clean_folder_button.pack(side="left")

        folder_label = self.ttk.Label(
            folder_row,
            textvariable=self.text_clean_folder_var,
            wraplength=540,
        )
        folder_label.pack(side="left", padx=(12, 0), fill="x", expand=True)

        running_header_label = self.ttk.Label(
            parent,
            text="Optional running header title to remove before page numbers:",
        )
        running_header_label.pack(anchor="w", pady=(6, 4))

        running_header_row = self.ttk.Frame(parent)
        running_header_row.pack(fill="x", pady=(0, 4))

        self.running_header_title_entry = self.ttk.Entry(
            running_header_row,
            textvariable=self.running_header_title_var,
        )
        self.running_header_title_entry.pack(side="left", fill="x", expand=True)

        running_header_hint = self.ttk.Label(
            parent,
            text=(
                "Example: enter 'The Hidden Evil' to remove line-start prefixes "
                "like 'The Hidden Evil  8' while keeping the paragraph text after it."
            ),
            wraplength=700,
        )
        running_header_hint.pack(anchor="w", pady=(0, 10))

        custom_label = self.ttk.Label(
            parent,
            text="Optional custom literal string to remove:",
        )
        custom_label.pack(anchor="w", pady=(6, 4))

        custom_text_frame = self.ttk.Frame(parent)
        custom_text_frame.pack(fill="both", expand=True, pady=(0, 6))

        self.custom_clean_text = self.tk.Text(
            custom_text_frame,
            height=6,
            wrap="word",
            undo=True,
        )
        custom_scrollbar = self.ttk.Scrollbar(
            custom_text_frame,
            orient="vertical",
            command=self.custom_clean_text.yview,
        )
        self.custom_clean_text.configure(yscrollcommand=custom_scrollbar.set)
        self.custom_clean_text.pack(side="left", fill="both", expand=True)
        custom_scrollbar.pack(side="right", fill="y")

        custom_hint = self.ttk.Label(
            parent,
            text=(
                "Matched as plain text, not regex. Leave empty to use only the "
                "built-in cleaners. Single-line text is removed as a standalone "
                "line or inline occurrence; multi-line text is removed as an exact block."
            ),
            wraplength=700,
        )
        custom_hint.pack(anchor="w", pady=(0, 12))

        self.text_clean_button = self.ttk.Button(
            parent,
            text="Clean TXT Files",
            command=self.start_text_cleaning,
        )
        self.text_clean_button.pack(anchor="w")


    def _build_pdf_tab(self, parent) -> None:
        description = self.ttk.Label(
            parent,
            text=(
                "Select a PDF, add one or more start/end page ranges, then split. "
                "Pages are 1-based and inclusive. Output files are saved in the "
                "same folder as the selected PDF."
            ),
            wraplength=660,
        )
        description.pack(anchor="w", pady=(0, 14))

        pdf_row = self.ttk.Frame(parent)
        pdf_row.pack(fill="x", pady=(0, 6))

        self.select_pdf_button = self.ttk.Button(
            pdf_row,
            text="Select PDF",
            command=self.select_pdf,
        )
        self.select_pdf_button.pack(side="left")

        pdf_label = self.ttk.Label(
            pdf_row,
            textvariable=self.pdf_var,
            wraplength=500,
        )
        pdf_label.pack(side="left", padx=(12, 0), fill="x", expand=True)

        pages_label = self.ttk.Label(
            parent,
            textvariable=self.pdf_pages_var,
        )
        pages_label.pack(anchor="w", pady=(0, 12))

        header_row = self.ttk.Frame(parent)
        header_row.pack(fill="x", pady=(0, 4))
        self.ttk.Label(header_row, text="Start page", width=14).pack(side="left")
        self.ttk.Label(header_row, text="End page", width=14).pack(side="left", padx=(8, 0))

        # Keep the action buttons visible even when many PDF parts are added.
        # Only the page-range rows scroll; the buttons stay fixed below.
        ranges_container = self.ttk.Frame(parent)
        ranges_container.pack(fill="both", expand=True, pady=(0, 10))

        self.pdf_parts_canvas = self.tk.Canvas(
            ranges_container,
            borderwidth=0,
            highlightthickness=0,
        )
        self.pdf_parts_scrollbar = self.ttk.Scrollbar(
            ranges_container,
            orient="vertical",
            command=self.pdf_parts_canvas.yview,
        )
        self.parts_frame = self.ttk.Frame(self.pdf_parts_canvas)
        self.pdf_parts_window = self.pdf_parts_canvas.create_window(
            (0, 0),
            window=self.parts_frame,
            anchor="nw",
        )
        self.pdf_parts_canvas.configure(yscrollcommand=self.pdf_parts_scrollbar.set)

        self.pdf_parts_canvas.pack(side="left", fill="both", expand=True)
        self.pdf_parts_scrollbar.pack(side="right", fill="y")

        self.parts_frame.bind(
            "<Configure>",
            self._on_pdf_parts_frame_configure,
        )
        self.pdf_parts_canvas.bind(
            "<Configure>",
            self._on_pdf_parts_canvas_configure,
        )
        self.pdf_parts_canvas.bind("<Enter>", self._bind_pdf_parts_mousewheel)
        self.pdf_parts_canvas.bind("<Leave>", self._unbind_pdf_parts_mousewheel)
        self.parts_frame.bind("<Enter>", self._bind_pdf_parts_mousewheel)
        self.parts_frame.bind("<Leave>", self._unbind_pdf_parts_mousewheel)

        action_row = self.ttk.Frame(parent)
        action_row.pack(fill="x")

        self.add_part_button = self.ttk.Button(
            action_row,
            text="+ Add Part",
            command=self.add_pdf_part_row,
        )
        self.add_part_button.pack(side="left")

        self.split_pdf_button = self.ttk.Button(
            action_row,
            text="Split PDF",
            command=self.start_pdf_split,
        )
        self.split_pdf_button.pack(side="left", padx=(10, 0))

        self.add_pdf_part_row(default_start="1", default_end="")

    def _on_pdf_parts_frame_configure(self, _event=None) -> None:
        """Update the PDF parts scrollable region after rows are added/removed."""
        if not hasattr(self, "pdf_parts_canvas"):
            return

        self.pdf_parts_canvas.configure(
            scrollregion=self.pdf_parts_canvas.bbox("all")
        )

    def _on_pdf_parts_canvas_configure(self, event) -> None:
        """Keep row widgets as wide as the visible canvas area."""
        if not hasattr(self, "pdf_parts_canvas") or not hasattr(self, "pdf_parts_window"):
            return

        self.pdf_parts_canvas.itemconfigure(
            self.pdf_parts_window,
            width=event.width,
        )

    def _bind_pdf_parts_mousewheel(self, _event=None) -> None:
        """Enable mouse/trackpad scrolling while the cursor is over PDF parts."""
        if not hasattr(self, "pdf_parts_canvas"):
            return

        self.pdf_parts_canvas.bind_all("<MouseWheel>", self._on_pdf_parts_mousewheel)
        self.pdf_parts_canvas.bind_all("<Button-4>", self._on_pdf_parts_mousewheel)
        self.pdf_parts_canvas.bind_all("<Button-5>", self._on_pdf_parts_mousewheel)

    def _unbind_pdf_parts_mousewheel(self, _event=None) -> None:
        """Avoid stealing scroll events when the cursor leaves PDF parts."""
        if not hasattr(self, "pdf_parts_canvas"):
            return

        self.pdf_parts_canvas.unbind_all("<MouseWheel>")
        self.pdf_parts_canvas.unbind_all("<Button-4>")
        self.pdf_parts_canvas.unbind_all("<Button-5>")

    def _on_pdf_parts_mousewheel(self, event) -> None:
        """Scroll the PDF parts area on macOS/Windows/Linux wheel events."""
        if not hasattr(self, "pdf_parts_canvas"):
            return

        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            delta = -1 * int(event.delta / 120) if event.delta else 0

        if delta:
            self.pdf_parts_canvas.yview_scroll(delta, "units")

    def select_folder(self) -> None:
        from tkinter import filedialog

        folder_name = filedialog.askdirectory(
            parent=self.root,
            title="Select folder containing images",
        )

        if not folder_name:
            return

        self.selected_folder = Path(folder_name)
        self.folder_var.set(str(self.selected_folder))
        self.status_var.set("Folder selected. Click JPG to TXT to start.")

    def select_pdf_text_folder(self) -> None:
        from tkinter import filedialog

        folder_name = filedialog.askdirectory(
            parent=self.root,
            title="Select folder containing PDFs",
        )

        if not folder_name:
            return

        self.selected_pdf_text_folder = Path(folder_name)
        self.pdf_text_folder_var.set(str(self.selected_pdf_text_folder))
        self.status_var.set("PDF folder selected. Click PDF to TXT to start.")

    def select_text_clean_folder(self) -> None:
        from tkinter import filedialog

        folder_name = filedialog.askdirectory(
            parent=self.root,
            title="Select folder containing TXT files",
        )

        if not folder_name:
            return

        self.selected_text_clean_folder = Path(folder_name)
        self.text_clean_folder_var.set(str(self.selected_text_clean_folder))
        self.status_var.set("TXT folder selected. Click Clean TXT Files to start.")


    def select_pdf(self) -> None:
        from tkinter import filedialog, messagebox

        file_name = filedialog.askopenfilename(
            parent=self.root,
            title="Select PDF to split",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )

        if not file_name:
            return

        pdf_path = Path(file_name)
        self.selected_pdf = pdf_path
        self.pdf_var.set(str(pdf_path))

        try:
            total_pages = read_pdf_total_pages(pdf_path)
        except Exception as exc:
            self.pdf_pages_var.set("")
            messagebox.showerror(
                title="Could not read PDF",
                message=str(exc),
                parent=self.root,
            )
            return

        self.pdf_pages_var.set(f"Total pages: {total_pages}")
        self.status_var.set("PDF selected. Add page ranges, then click Split PDF.")

        if len(self.pdf_part_rows) == 1:
            _, start_var, end_var = self.pdf_part_rows[0]
            if hasattr(start_var, "set") and hasattr(end_var, "set"):
                start_var.set("1")
                end_var.set(str(total_pages))

    def add_pdf_part_row(self, default_start: str = "", default_end: str = "") -> None:
        row_frame = self.ttk.Frame(self.parts_frame)
        row_frame.pack(fill="x", pady=(0, 6))

        start_var = self.tk.StringVar(value=default_start)
        end_var = self.tk.StringVar(value=default_end)

        start_entry = self.ttk.Entry(row_frame, textvariable=start_var, width=14)
        start_entry.pack(side="left")

        end_entry = self.ttk.Entry(row_frame, textvariable=end_var, width=14)
        end_entry.pack(side="left", padx=(8, 0))

        remove_button = self.ttk.Button(
            row_frame,
            text="Remove",
            command=lambda: self.remove_pdf_part_row(row_frame),
        )
        remove_button.pack(side="left", padx=(8, 0))

        self.pdf_part_rows.append((row_frame, start_var, end_var))
        self._refresh_remove_buttons()

        if hasattr(self, "pdf_parts_canvas"):
            self.root.after_idle(
                lambda: self.pdf_parts_canvas.yview_moveto(1.0)
            )

    def remove_pdf_part_row(self, row_frame) -> None:
        if len(self.pdf_part_rows) <= 1:
            return

        for index, (frame, _start_var, _end_var) in enumerate(self.pdf_part_rows):
            if frame is row_frame:
                frame.destroy()
                del self.pdf_part_rows[index]
                break

        self._refresh_remove_buttons()

    def _refresh_remove_buttons(self) -> None:
        # Keep at least one row. Disable the only row's Remove button.
        single_row = len(self.pdf_part_rows) <= 1

        for frame, _start_var, _end_var in self.pdf_part_rows:
            for child in frame.winfo_children():
                if isinstance(child, self.ttk.Button) and child.cget("text") == "Remove":
                    child.configure(state="disabled" if single_row else "normal")

    def collect_pdf_ranges(self) -> list[PDFPartRange]:
        ranges: list[PDFPartRange] = []

        for index, (_frame, start_var, end_var) in enumerate(self.pdf_part_rows, start=1):
            start_raw = start_var.get().strip()
            end_raw = end_var.get().strip()

            if not start_raw or not end_raw:
                raise PDFSplitterError(f"Part {index}: start and end pages are required.")

            try:
                start_page = int(start_raw)
                end_page = int(end_raw)
            except ValueError as exc:
                raise PDFSplitterError(
                    f"Part {index}: start and end pages must be whole numbers."
                ) from exc

            ranges.append(PDFPartRange(start_page=start_page, end_page=end_page))

        return ranges

    def start_conversion(self) -> None:
        from tkinter import messagebox

        if self.is_running:
            return

        if self.selected_folder is None:
            messagebox.showwarning(
                title="No folder selected",
                message="Please click Select Folder first.",
                parent=self.root,
            )
            return

        try:
            model_token_capacity = int(self.capacity_var.get().strip())
        except ValueError:
            messagebox.showerror(
                title="Invalid token capacity",
                message="Model token capacity must be a number.",
                parent=self.root,
            )
            return

        if model_token_capacity < 1_000:
            messagebox.showerror(
                title="Invalid token capacity",
                message="Model token capacity must be at least 1000.",
                parent=self.root,
            )
            return

        self._set_running_state(True, "Starting OCR...")

        worker = threading.Thread(
            target=self._run_conversion_worker,
            args=(self.selected_folder, model_token_capacity),
            daemon=True,
        )
        worker.start()

    def _run_conversion_worker(
        self,
        folder: Path,
        model_token_capacity: int,
    ) -> None:
        try:
            result = process_folder(
                folder=folder,
                model_token_capacity=model_token_capacity,
                progress_callback=self._thread_safe_progress,
            )
        except Exception as exc:
            self.root.after(0, self._show_error, exc)
            return

        self.root.after(0, self._show_ocr_success, result)

    def start_pdf_text_extraction(self) -> None:
        from tkinter import messagebox

        if self.is_running:
            return

        if self.selected_pdf_text_folder is None:
            messagebox.showwarning(
                title="No PDF folder selected",
                message="Please click Select Folder first.",
                parent=self.root,
            )
            return

        try:
            model_token_capacity = int(self.capacity_var.get().strip())
        except ValueError:
            messagebox.showerror(
                title="Invalid token capacity",
                message="Model token capacity must be a number.",
                parent=self.root,
            )
            return

        if model_token_capacity < 1_000:
            messagebox.showerror(
                title="Invalid token capacity",
                message="Model token capacity must be at least 1000.",
                parent=self.root,
            )
            return

        self._set_running_state(True, "Starting PDF text extraction...")

        worker = threading.Thread(
            target=self._run_pdf_text_worker,
            args=(self.selected_pdf_text_folder, model_token_capacity),
            daemon=True,
        )
        worker.start()

    def _run_pdf_text_worker(
        self,
        folder: Path,
        model_token_capacity: int,
    ) -> None:
        try:
            result = process_pdf_folder_to_text(
                folder=folder,
                model_token_capacity=model_token_capacity,
                progress_callback=self._thread_safe_progress,
            )
        except Exception as exc:
            self.root.after(0, self._show_error, exc)
            return

        self.root.after(0, self._show_pdf_text_success, result)

    def start_text_cleaning(self) -> None:
        from tkinter import messagebox

        if self.is_running:
            return

        if self.selected_text_clean_folder is None:
            messagebox.showwarning(
                title="No TXT folder selected",
                message="Please click Select Folder first.",
                parent=self.root,
            )
            return

        custom_clean_string = ""
        if self.custom_clean_text is not None:
            custom_clean_string = self.custom_clean_text.get("1.0", "end-1c")

        running_header_title = self.running_header_title_var.get().strip()

        self._set_running_state(True, "Starting TXT cleaning...")

        worker = threading.Thread(
            target=self._run_text_clean_worker,
            args=(
                self.selected_text_clean_folder,
                custom_clean_string,
                running_header_title,
            ),
            daemon=True,
        )
        worker.start()

    def _run_text_clean_worker(
        self,
        folder: Path,
        custom_clean_string: str,
        running_header_title: str,
    ) -> None:
        try:
            result = clean_txt_files_in_folder(
                folder=folder,
                custom_clean_string=custom_clean_string,
                running_header_title=running_header_title,
                progress_callback=self._thread_safe_progress,
            )
        except Exception as exc:
            self.root.after(0, self._show_error, exc)
            return

        self.root.after(0, self._show_text_clean_success, result)


    def start_pdf_split(self) -> None:
        from tkinter import messagebox

        if self.is_running:
            return

        if self.selected_pdf is None:
            messagebox.showwarning(
                title="No PDF selected",
                message="Please click Select PDF first.",
                parent=self.root,
            )
            return

        try:
            ranges = self.collect_pdf_ranges()
        except Exception as exc:
            messagebox.showerror(
                title="Invalid page ranges",
                message=str(exc),
                parent=self.root,
            )
            return

        self._set_running_state(True, "Starting PDF split...")

        worker = threading.Thread(
            target=self._run_pdf_split_worker,
            args=(self.selected_pdf, ranges),
            daemon=True,
        )
        worker.start()

    def _run_pdf_split_worker(
        self,
        pdf_path: Path,
        ranges: list[PDFPartRange],
    ) -> None:
        try:
            result = split_pdf(
                pdf_path=pdf_path,
                ranges=ranges,
                progress_callback=self._thread_safe_progress,
            )
        except Exception as exc:
            self.root.after(0, self._show_error, exc)
            return

        self.root.after(0, self._show_pdf_success, result)

    def _thread_safe_progress(self, message: str, value: int, maximum: int) -> None:
        self.root.after(0, self._update_progress, message, value, maximum)

    def _update_progress(self, message: str, value: int, maximum: int) -> None:
        self.status_var.set(message)
        self.progress_bar["maximum"] = max(maximum, 1)
        self.progress_bar["value"] = value

    def _show_ocr_success(self, result: ProcessingResult) -> None:
        from tkinter import messagebox

        self._update_progress("Done.", 1, 1)
        self._set_running_state(False)

        messagebox.showinfo(
            title="Done",
            message=(
                f"Created {len(result.output_files)} chunk file(s).\n"
                f"Token budget per chunk: {result.token_budget}\n"
                f"Processed images: {result.processed_images}\n"
                f"Skipped images: {result.skipped_images}\n\n"
                f"Files were saved in:\n{self.selected_folder}"
            ),
            parent=self.root,
        )

    def _show_pdf_text_success(self, result: PDFTextResult) -> None:
        from tkinter import messagebox

        self._update_progress("Done.", 1, 1)
        self._set_running_state(False)

        messagebox.showinfo(
            title="Done",
            message=(
                f"Created {len(result.output_files)} text chunk file(s).\n"
                f"Token budget per chunk: {result.token_budget}\n"
                f"Processed PDFs: {result.processed_pdfs}\n"
                f"Skipped PDFs: {result.skipped_pdfs}\n"
                f"Processed pages: {result.processed_pages}\n"
                f"Direct text pages: {result.direct_text_pages}\n"
                f"OCR pages: {result.ocr_pages}\n\n"
                f"Files were saved in:\n{self.selected_pdf_text_folder}"
            ),
            parent=self.root,
        )

    def _show_text_clean_success(self, result: TextCleanResult) -> None:
        from tkinter import messagebox

        self._update_progress("Done.", 1, 1)
        self._set_running_state(False)

        warning = ""
        if result.errors:
            warning = f"\nWarnings: {len(result.errors)} file(s) could not be cleaned."

        messagebox.showinfo(
            title="Done",
            message=(
                f"Scanned TXT files: {result.scanned_files}\n"
                f"Changed TXT files: {result.changed_files}\n"
                f"Removed marker/custom items: {result.removed_marker_lines}\n"
                f"Removed following empty lines: {result.removed_empty_lines}\n"
                f"{warning}\n\n"
                f"Files were cleaned in-place in:\n{self.selected_text_clean_folder}"
            ),
            parent=self.root,
        )


    def _show_pdf_success(self, result: PDFSplitResult) -> None:
        from tkinter import messagebox

        self._update_progress("Done.", 1, 1)
        self._set_running_state(False)

        files_preview = "\n".join(path.name for path in result.output_files[:8])
        if len(result.output_files) > 8:
            files_preview += f"\n...and {len(result.output_files) - 8} more"

        messagebox.showinfo(
            title="Done",
            message=(
                f"Created {len(result.output_files)} PDF part(s).\n"
                f"Source pages: {result.total_pages}\n\n"
                f"Saved in:\n{result.source_pdf.parent}\n\n"
                f"Files:\n{files_preview}"
            ),
            parent=self.root,
        )

    def _show_error(self, error: Exception) -> None:
        from tkinter import messagebox

        self._set_running_state(False)
        self.status_var.set("Error.")
        messagebox.showerror(
            title="Error",
            message=str(error),
            parent=self.root,
        )

    def _set_running_state(self, is_running: bool, message: str | None = None) -> None:
        self.is_running = is_running
        state = "disabled" if is_running else "normal"

        self.select_folder_button.configure(state=state)
        self.convert_button.configure(state=state)
        self.select_pdf_text_folder_button.configure(state=state)
        self.pdf_text_button.configure(state=state)
        self.select_text_clean_folder_button.configure(state=state)
        self.text_clean_button.configure(state=state)
        if self.custom_clean_text is not None:
            self.custom_clean_text.configure(state=state)
        if self.running_header_title_entry is not None:
            self.running_header_title_entry.configure(state=state)
        self.select_pdf_button.configure(state=state)
        self.add_part_button.configure(state=state)
        self.split_pdf_button.configure(state=state)

        for frame, _start_var, _end_var in self.pdf_part_rows:
            for child in frame.winfo_children():
                try:
                    child.configure(state=state)
                except Exception:
                    pass

        if not is_running:
            self._refresh_remove_buttons()

        if message is not None:
            self.status_var.set(message)

        if is_running:
            self.progress_bar["value"] = 0


def run_gui() -> None:
    """Start the GUI without opening prompts at startup."""
    app = BookUtilsApp()
    app.root.mainloop()


def main() -> None:
    ensure_dependencies()
    run_gui()


if __name__ == "__main__":
    main()
