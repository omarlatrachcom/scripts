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
   - Also removes one empty line immediately following each removed metadata line.

Dependency handling:
- Checks missing modules.
- Installs missing Python packages idempotently and automatically.
"""

from __future__ import annotations

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
TXT_CLEAN_FOOTER_LINE_PATTERNS = (
    re.compile(r"^\s*\d+\s+min(?:ute)?s?\s+left\s+in\s+chapter\s*$", re.IGNORECASE),
    re.compile(r"^\s*learning\s+reading\s+speed\b.*$", re.IGNORECASE),
    re.compile(r"^\s*\d+\s*%\s*$", re.IGNORECASE),
)
TXT_CLEAN_INLINE_NOISE_PATTERNS = (
    re.compile(r"\s*\b\d+\s+min(?:ute)?s?\s+left\s+in\s+chapter\b\s*", re.IGNORECASE),
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
        or any(
            pattern.match(normalized_line)
            for pattern in TXT_CLEAN_FOOTER_LINE_PATTERNS
        )
    )


def strip_embedded_clean_noise(line: str) -> tuple[str, bool]:
    """Remove Kindle footer noise inside a line without deleting the line.

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

    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()

    if not cleaned:
        return "", cleaned != line_body.strip()

    return cleaned + line_ending, cleaned != line_body.strip()


def clean_txt_content(text: str) -> tuple[str, int, int]:
    """Remove generated metadata/footer noise without erasing real paragraphs."""
    lines = text.splitlines(keepends=True)
    cleaned_lines: list[str] = []
    removed_marker_lines = 0
    removed_empty_lines = 0
    index = 0

    while index < len(lines):
        line = lines[index]

        if line_is_standalone_clean_marker(line):
            removed_marker_lines += 1
            index += 1

            if index < len(lines) and not lines[index].strip():
                removed_empty_lines += 1
                index += 1

            continue

        cleaned_line, changed = strip_embedded_clean_noise(line)
        if changed:
            removed_marker_lines += 1

        if cleaned_line:
            cleaned_lines.append(cleaned_line)

        index += 1

    return "".join(cleaned_lines), removed_marker_lines, removed_empty_lines


def clean_txt_files_in_folder(
    folder: Path,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> TextCleanResult:
    """Clean all TXT files directly inside a selected folder."""
    txt_files = find_txt_files(folder)

    if not txt_files:
        raise TextCleanerError("No TXT files were found in the selected folder.")

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
            original_text
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


def lines_to_paragraphs(lines: list[OCRLine]) -> str:
    """Group OCR lines into simple paragraphs."""
    if not lines:
        return ""

    ordered = sorted(lines, key=lambda line: (-line.top, line.x))
    median_height = sorted(line.height for line in ordered)[len(ordered) // 2]
    paragraph_gap_threshold = max(median_height * 1.6, 0.025)

    paragraphs: list[list[str]] = []
    current: list[str] = []
    previous: OCRLine | None = None

    for line in ordered:
        if previous is not None:
            vertical_gap = previous.bottom - line.top

            if vertical_gap > paragraph_gap_threshold and current:
                paragraphs.append(current)
                current = []

        current.append(line.text)
        previous = line

    if current:
        paragraphs.append(current)

    return "\n\n".join(" ".join(paragraph).strip() for paragraph in paragraphs).strip()


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


def render_pdf_page_to_image(
    pdf_document,
    page_index: int,
    temp_dir: Path,
    pdf_path: Path,
    dpi: int = 220,
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

                try:
                    direct_text = normalize_pdf_text(page.extract_text() or "")
                except Exception as exc:
                    direct_text = ""
                    errors.append(
                        f"{pdf_path.name} page {page_index + 1}: direct text failed: {exc}"
                    )

                if has_useful_text(direct_text):
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
                    except Exception as exc:
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
            f"{safe_stem}_{PDF_OUTPUT_SUFFIX}_{index:03d}_"
            f"p{part.start_page:03d}-p{part.end_page:03d}.pdf"
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
                "'1 min left in chapter', 'Learning reading speed', and page percent "
                "lines. If Kindle footer text was merged into a real paragraph, it "
                "strips only the footer text and keeps the paragraph."
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

        self.parts_frame = self.ttk.Frame(parent)
        self.parts_frame.pack(fill="x", pady=(0, 10))

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

        self._set_running_state(True, "Starting TXT cleaning...")

        worker = threading.Thread(
            target=self._run_text_clean_worker,
            args=(self.selected_text_clean_folder,),
            daemon=True,
        )
        worker.start()

    def _run_text_clean_worker(self, folder: Path) -> None:
        try:
            result = clean_txt_files_in_folder(
                folder=folder,
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
                f"Removed marker lines: {result.removed_marker_lines}\n"
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
