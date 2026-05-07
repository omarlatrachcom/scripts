#!/usr/bin/env python3
"""
macOS GUI OCR chunker.

UI flow:
1. App opens a normal window.
2. User clicks "Select Folder".
3. User clicks "JPG to TXT".
4. Text is extracted from images and written as token-safe .txt chunks
   into the same selected folder.

OCR engine:
- Uses Apple's built-in Vision OCR through PyObjC.
- Best run from a virtual environment created by your Automator launcher.

Dependency handling:
- Checks missing modules.
- Installs missing Python packages idempotently.
"""

from __future__ import annotations

import importlib.util
import json
import platform
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


DEFAULT_MODEL_TOKEN_CAPACITY = 196_000
OUTPUT_PREFIX = "chatgpt_ocr_chunk"
ERRORS_FILENAME = "chatgpt_ocr_errors.txt"
MANIFEST_FILENAME = "chatgpt_ocr_manifest.json"

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
}

SENTENCE_END_RE = re.compile(r"[.!?؟。！？…]['\")\]]*$")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?؟。！？…])\s+")


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


class OCRChunkerError(RuntimeError):
    """Raised for user-facing OCR chunker failures."""


def ensure_dependencies() -> None:
    """Install missing dependencies only when needed."""
    if platform.system() != "Darwin":
        raise OCRChunkerError("This script is macOS-only because it uses Apple Vision OCR.")

    missing_packages: list[str] = []

    for module_name, package_name in REQUIRED_MODULES.items():
        if importlib.util.find_spec(module_name) is None:
            missing_packages.append(package_name)

    if not missing_packages:
        return

    ensure_pip()

    for package_name in sorted(set(missing_packages)):
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--user", package_name]
        )

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


class OCRChunkerApp:
    """Tkinter GUI application."""

    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.selected_folder: Path | None = None
        self.is_running = False

        self.root = tk.Tk()
        self.root.title("JPG to TXT OCR Chunker")
        self.root.geometry("620x300")
        self.root.minsize(620, 300)

        self.status_var = tk.StringVar(value="Select a folder, then click JPG to TXT.")
        self.folder_var = tk.StringVar(value="No folder selected")
        self.capacity_var = tk.StringVar(value=str(DEFAULT_MODEL_TOKEN_CAPACITY))

        self._build_ui()

    def _build_ui(self) -> None:
        frame = self.ttk.Frame(self.root, padding=18)
        frame.pack(fill="both", expand=True)

        title = self.ttk.Label(
            frame,
            text="JPG to TXT OCR Chunker",
            font=("Helvetica", 18, "bold"),
        )
        title.pack(anchor="w", pady=(0, 8))

        description = self.ttk.Label(
            frame,
            text=(
                "Choose a folder containing images. The app extracts text and writes "
                "consecutive .txt chunks into the same folder."
            ),
            wraplength=560,
        )
        description.pack(anchor="w", pady=(0, 14))

        folder_row = self.ttk.Frame(frame)
        folder_row.pack(fill="x", pady=(0, 10))

        self.select_button = self.ttk.Button(
            folder_row,
            text="Select Folder",
            command=self.select_folder,
        )
        self.select_button.pack(side="left")

        folder_label = self.ttk.Label(
            folder_row,
            textvariable=self.folder_var,
            wraplength=420,
        )
        folder_label.pack(side="left", padx=(12, 0), fill="x", expand=True)

        capacity_row = self.ttk.Frame(frame)
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

        action_row = self.ttk.Frame(frame)
        action_row.pack(fill="x", pady=(0, 14))

        self.convert_button = self.ttk.Button(
            action_row,
            text="JPG to TXT",
            command=self.start_conversion,
        )
        self.convert_button.pack(side="left")

        self.progress_bar = self.ttk.Progressbar(
            frame,
            mode="determinate",
        )
        self.progress_bar.pack(fill="x", pady=(0, 10))

        status_label = self.ttk.Label(
            frame,
            textvariable=self.status_var,
            wraplength=560,
        )
        status_label.pack(anchor="w")

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

        self._set_running_state(True)

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

        self.root.after(0, self._show_success, result)

    def _thread_safe_progress(self, message: str, value: int, maximum: int) -> None:
        self.root.after(0, self._update_progress, message, value, maximum)

    def _update_progress(self, message: str, value: int, maximum: int) -> None:
        self.status_var.set(message)
        self.progress_bar["maximum"] = max(maximum, 1)
        self.progress_bar["value"] = value

    def _show_success(self, result: ProcessingResult) -> None:
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

    def _show_error(self, error: Exception) -> None:
        from tkinter import messagebox

        self._set_running_state(False)
        self.status_var.set("Error.")
        messagebox.showerror(
            title="Error",
            message=str(error),
            parent=self.root,
        )

    def _set_running_state(self, is_running: bool) -> None:
        self.is_running = is_running
        state = "disabled" if is_running else "normal"

        self.select_button.configure(state=state)
        self.convert_button.configure(state=state)

        if is_running:
            self.status_var.set("Starting OCR...")
            self.progress_bar["value"] = 0


def run_gui() -> None:
    """Start the GUI without opening prompts at startup."""
    app = OCRChunkerApp()
    app.root.mainloop()


def main() -> None:
    ensure_dependencies()
    run_gui()


if __name__ == "__main__":
    main()
