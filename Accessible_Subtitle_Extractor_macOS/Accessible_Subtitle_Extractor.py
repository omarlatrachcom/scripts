#!/usr/bin/env python3
"""
Accessible Subtitle Extractor for macOS

Features
--------
- Select one or many video/audio files, starting in ~/Downloads.
- Prefer an embedded text subtitle track, or transcribe speech to SRT.
- Uses a keyboard-accessible PySide6 interface with explicit accessible names.
- Optional spoken status announcements through macOS `say`.
- Creates and maintains a private virtual environment automatically.
- Downloads Python dependencies and Whisper models only when missing.
- Skips existing non-empty SRT files by default for idempotent repeat runs.
- Writes through a temporary file and atomically replaces the destination.

Run with Python 3.10 or newer:
    python3 Accessible_Subtitle_Extractor.py
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
import traceback
import venv
from pathlib import Path

APP_NAME = "Accessible Subtitle Extractor"
APP_SLUG = "AccessibleSubtitleExtractor"
APP_SUPPORT = Path.home() / "Library" / "Application Support" / APP_SLUG
VENV_DIR = APP_SUPPORT / "venv"
LOG_DIR = Path.home() / "Library" / "Logs"
BOOTSTRAP_LOG = LOG_DIR / f"{APP_SLUG}-bootstrap.log"
RUNTIME_LOG = LOG_DIR / f"{APP_SLUG}.log"
BOOTSTRAP_ENV = "ACCESSIBLE_SUBTITLE_EXTRACTOR_VENV"

# Installed only inside the app's private virtual environment.
DEPENDENCIES = (
    "PySide6-Essentials>=6.7",
    "faster-whisper>=1.2",
    "imageio-ffmpeg>=0.6",
)
REQUIRED_IMPORTS = ("PySide6", "faster_whisper", "imageio_ffmpeg")


def _apple_script_string(value: str) -> str:
    """Return a safely quoted AppleScript string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def native_notification(message: str) -> None:
    """Best-effort macOS notification before the GUI dependency exists."""
    if platform.system() != "Darwin":
        return
    script = (
        f"display notification {_apple_script_string(message)} "
        f"with title {_apple_script_string(APP_NAME)}"
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def native_error_dialog(message: str) -> None:
    """Best-effort native error dialog used if GUI setup fails."""
    if platform.system() != "Darwin":
        return
    script = (
        f"display alert {_apple_script_string(APP_NAME)} "
        f"message {_apple_script_string(message)} as critical"
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def _venv_python() -> Path:
    return VENV_DIR / "bin" / "python3"


def _imports_work(python_executable: Path) -> bool:
    statement = "; ".join(f"import {name}" for name in REQUIRED_IMPORTS)
    result = subprocess.run(
        [str(python_executable), "-c", statement],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def bootstrap_private_environment() -> None:
    """
    Create an isolated environment, install missing dependencies, and relaunch.

    Repeated launches only perform a quick import check; pip is not invoked when
    all dependencies are already usable.
    """
    if os.environ.get(BOOTSTRAP_ENV) == "1":
        return

    if sys.version_info < (3, 10):
        message = (
            "Python 3.10 or newer is required. Install a current Python 3 for "
            "macOS, then launch this script again."
        )
        native_error_dialog(message)
        raise SystemExit(message)

    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    python_executable = _venv_python()

    try:
        if not python_executable.exists():
            native_notification("Creating the private application environment.")
            venv.EnvBuilder(with_pip=True, clear=False, symlinks=True).create(VENV_DIR)

        if not _imports_work(python_executable):
            native_notification("Installing missing accessibility and transcription components.")
            with BOOTSTRAP_LOG.open("a", encoding="utf-8") as log:
                log.write("\n\n=== Bootstrap started " + time.strftime("%Y-%m-%d %H:%M:%S") + " ===\n")
                log.flush()

                # A pip upgrade is helpful but non-fatal; dependency installation is authoritative.
                subprocess.run(
                    [
                        str(python_executable),
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "--no-input",
                        "--upgrade",
                        "pip",
                        "setuptools",
                        "wheel",
                    ],
                    check=False,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                install = subprocess.run(
                    [
                        str(python_executable),
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "--no-input",
                        *DEPENDENCIES,
                    ],
                    check=False,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                log.flush()

            if install.returncode != 0 or not _imports_work(python_executable):
                raise RuntimeError(
                    "Automatic dependency installation failed. "
                    f"Details were written to {BOOTSTRAP_LOG}."
                )

        environment = os.environ.copy()
        environment[BOOTSTRAP_ENV] = "1"
        os.execve(
            str(python_executable),
            [str(python_executable), str(Path(__file__).resolve()), *sys.argv[1:]],
            environment,
        )
    except Exception as exc:
        message = f"The application could not prepare its private environment.\n\n{exc}"
        native_error_dialog(message)
        print(message, file=sys.stderr)
        raise SystemExit(1) from exc


bootstrap_private_environment()

# Third-party imports happen only after the private environment is active.
import logging
import re
import textwrap
import threading
import uuid
from dataclasses import dataclass
from typing import Iterable

import imageio_ffmpeg
from faster_whisper import WhisperModel
from PySide6.QtCore import QSettings, QThread, Qt, QObject, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QDragEnterEvent, QDropEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=RUNTIME_LOG,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger(APP_SLUG)

SUPPORTED_SUFFIXES = {
    ".3gp",
    ".aac",
    ".aiff",
    ".alac",
    ".avi",
    ".flac",
    ".m4a",
    ".m4v",
    ".mka",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".oga",
    ".ogg",
    ".opus",
    ".ts",
    ".wav",
    ".webm",
    ".wma",
    ".wmv",
}

FILE_FILTER = (
    "Video and audio files "
    "(*.3gp *.aac *.aiff *.alac *.avi *.flac *.m4a *.m4v *.mka *.mkv "
    "*.mov *.mp3 *.mp4 *.mpeg *.mpg *.oga *.ogg *.opus *.ts *.wav *.webm *.wma *.wmv);;"
    "All files (*)"
)

MODE_PREFER = "prefer"
MODE_TRANSCRIBE = "transcribe"
MODE_EMBEDDED_ONLY = "embedded_only"


class CancelledError(Exception):
    """Raised internally when the user requests cancellation."""


@dataclass(frozen=True)
class JobOptions:
    model_name: str
    language: str | None
    mode: str
    replace_existing: bool


class CueBuilder:
    """Convert Whisper word timestamps into readable two-line subtitle cues."""

    MAX_CHARACTERS = 84
    MAX_DURATION = 6.5
    MIN_PUNCTUATION_BREAK_DURATION = 1.2
    PUNCTUATION = (".", "!", "?", "…", "؟", "。", "！", "？")

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._start: float | None = None
        self._end: float | None = None

    @staticmethod
    def _joined(parts: Iterable[str]) -> str:
        # faster-whisper word tokens normally retain their leading whitespace.
        return "".join(parts).strip()

    def _flush(self) -> tuple[float, float, str] | None:
        text = self._joined(self._parts)
        if not text or self._start is None or self._end is None:
            self._parts.clear()
            self._start = None
            self._end = None
            return None

        start = max(0.0, self._start)
        end = max(start + 0.10, self._end)
        cue = (start, end, text)
        self._parts.clear()
        self._start = None
        self._end = None
        return cue

    def consume(self, segment: object) -> list[tuple[float, float, str]]:
        emitted: list[tuple[float, float, str]] = []
        words = getattr(segment, "words", None) or []

        if not words:
            pending = self._flush()
            if pending:
                emitted.append(pending)
            text = str(getattr(segment, "text", "")).strip()
            if text:
                start = float(getattr(segment, "start", 0.0))
                end = float(getattr(segment, "end", start + 0.10))
                emitted.append((max(0.0, start), max(start + 0.10, end), text))
            return emitted

        for word in words:
            raw = str(getattr(word, "word", ""))
            clean = raw.strip()
            if not clean:
                continue

            word_start = float(getattr(word, "start", getattr(segment, "start", 0.0)))
            word_end = float(getattr(word, "end", getattr(segment, "end", word_start + 0.10)))

            if self._start is None:
                self._start = word_start

            candidate = self._joined([*self._parts, raw])
            candidate_duration = word_end - self._start
            limit_reached = bool(self._parts) and (
                len(candidate) > self.MAX_CHARACTERS or candidate_duration > self.MAX_DURATION
            )

            if limit_reached:
                pending = self._flush()
                if pending:
                    emitted.append(pending)
                self._start = word_start

            self._parts.append(raw)
            self._end = word_end

            current_text = self._joined(self._parts)
            current_duration = (self._end or word_end) - (self._start or word_start)
            punctuation_break = clean.endswith(self.PUNCTUATION)
            if (
                punctuation_break
                and current_duration >= self.MIN_PUNCTUATION_BREAK_DURATION
                and len(current_text) >= 18
            ):
                pending = self._flush()
                if pending:
                    emitted.append(pending)

        return emitted

    def finish(self) -> tuple[float, float, str] | None:
        return self._flush()


def srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def wrap_subtitle(text: str) -> str:
    normalized = re.sub(r"[ \t]+", " ", text).strip()
    lines = textwrap.wrap(
        normalized,
        width=42,
        break_long_words=False,
        break_on_hyphens=False,
    )
    if len(lines) <= 2:
        return "\n".join(lines) if lines else normalized

    # CueBuilder normally keeps the text under 84 characters. This fallback
    # merges any unusual third line into the second without discarding words.
    return lines[0] + "\n" + " ".join(lines[1:])


def temporary_srt_path(target: Path) -> Path:
    return target.with_name(f".{target.stem}.{uuid.uuid4().hex}.tmp.srt")


class SubtitleWorker(QObject):
    progress = Signal(int)
    status = Signal(str, bool)  # message, announce aloud
    log = Signal(str)
    finished = Signal(dict)

    def __init__(self, files: list[Path], options: JobOptions) -> None:
        super().__init__()
        self.files = files
        self.options = options
        self.cancel_event = threading.Event()
        self._model: WhisperModel | None = None
        self._ffmpeg_executable: str | None = None

    def cancel(self) -> None:
        self.cancel_event.set()

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise CancelledError

    def _overall_progress(self, file_index: int, fraction: float) -> int:
        total = max(1, len(self.files))
        clamped = min(1.0, max(0.0, fraction))
        return int(round(((file_index + clamped) / total) * 100))

    def _emit_log(self, message: str) -> None:
        LOGGER.info(message)
        self.log.emit(message)

    def _run_cancellable_process(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        while process.poll() is None:
            if self.cancel_event.is_set():
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise CancelledError
            time.sleep(0.10)
        stdout, stderr = process.communicate()
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def _extract_embedded_subtitle(self, source: Path, target: Path) -> tuple[bool, str]:
        self._check_cancelled()
        if self._ffmpeg_executable is None:
            self._ffmpeg_executable = imageio_ffmpeg.get_ffmpeg_exe()

        last_reason = "No extractable text subtitle track was found."

        # Try subtitle tracks in order. A video can contain a bitmap track first
        # and an SRT-convertible text track later, so stopping at 0:s:0 would be
        # unnecessarily fragile. The first missing index ends the search.
        for subtitle_index in range(32):
            temporary = temporary_srt_path(target)
            command = [
                self._ffmpeg_executable,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-map",
                f"0:s:{subtitle_index}",
                "-c:s",
                "srt",
                str(temporary),
            ]

            try:
                result = self._run_cancellable_process(command)
                if result.returncode == 0 and temporary.exists() and temporary.stat().st_size > 0:
                    os.replace(temporary, target)
                    return (
                        True,
                        f"Extracted embedded text subtitle track {subtitle_index + 1}.",
                    )

                details = (result.stderr or "").strip().splitlines()
                last_reason = details[-1] if details else last_reason
                if "matches no streams" in (result.stderr or "").lower():
                    break
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

        return False, last_reason

    def _prepare_model(self) -> WhisperModel:
        if self._model is not None:
            return self._model

        self.status.emit(
            f"Preparing the {self.options.model_name} speech recognition model.",
            True,
        )
        self._emit_log(
            "Preparing speech recognition. A missing model is downloaded automatically "
            f"to {APP_SUPPORT / 'models'}."
        )
        self._check_cancelled()

        model_directory = APP_SUPPORT / "models"
        model_directory.mkdir(parents=True, exist_ok=True)
        cpu_threads = max(1, (os.cpu_count() or 2) - 1)
        self._model = WhisperModel(
            self.options.model_name,
            device="cpu",
            compute_type="int8",
            cpu_threads=cpu_threads,
            download_root=str(model_directory),
        )
        return self._model

    def _transcribe(self, source: Path, target: Path, file_index: int) -> None:
        model = self._prepare_model()
        self._check_cancelled()
        self.status.emit(f"Transcribing {source.name}.", True)
        self._emit_log(f"Transcribing speech: {source}")

        segments, info = model.transcribe(
            str(source),
            language=self.options.language,
            beam_size=5,
            vad_filter=True,
            word_timestamps=True,
            condition_on_previous_text=True,
        )
        duration = max(0.1, float(getattr(info, "duration", 0.1)))
        detected_language = getattr(info, "language", None)
        if detected_language:
            probability = float(getattr(info, "language_probability", 0.0))
            self._emit_log(
                f"Detected language for {source.name}: {detected_language} "
                f"({probability:.0%} confidence)."
            )

        temporary = temporary_srt_path(target)
        cue_builder = CueBuilder()
        cue_number = 0

        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as output:
                for segment in segments:
                    self._check_cancelled()
                    for start, end, text in cue_builder.consume(segment):
                        cue_number += 1
                        output.write(
                            f"{cue_number}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n"
                            f"{wrap_subtitle(text)}\n\n"
                        )

                    segment_end = float(getattr(segment, "end", 0.0))
                    self.progress.emit(
                        self._overall_progress(file_index, min(0.99, segment_end / duration))
                    )

                final_cue = cue_builder.finish()
                if final_cue:
                    cue_number += 1
                    start, end, text = final_cue
                    output.write(
                        f"{cue_number}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n"
                        f"{wrap_subtitle(text)}\n\n"
                    )

                output.flush()
                os.fsync(output.fileno())

            if cue_number == 0 or temporary.stat().st_size == 0:
                raise RuntimeError("No speech could be converted into subtitle cues.")

            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @Slot()
    def run(self) -> None:
        result: dict[str, object] = {
            "completed": 0,
            "skipped": 0,
            "failed": 0,
            "cancelled": False,
            "outputs": [],
            "errors": [],
        }

        try:
            for file_index, source in enumerate(self.files):
                self._check_cancelled()
                self.progress.emit(self._overall_progress(file_index, 0.0))
                target = source.with_suffix(".srt")
                self.status.emit(f"Processing {source.name}.", False)
                self._emit_log(f"Processing: {source}")

                if not source.exists() or not source.is_file():
                    result["failed"] = int(result["failed"]) + 1
                    error = f"{source.name}: the source file no longer exists."
                    result["errors"].append(error)
                    self._emit_log(error)
                    continue

                if target.exists() and target.stat().st_size > 0 and not self.options.replace_existing:
                    result["skipped"] = int(result["skipped"]) + 1
                    self._emit_log(f"Skipped existing output: {target}")
                    self.progress.emit(self._overall_progress(file_index, 1.0))
                    continue

                try:
                    created = False
                    embedded_reason = ""

                    if self.options.mode in (MODE_PREFER, MODE_EMBEDDED_ONLY):
                        self.status.emit(
                            f"Checking {source.name} for an embedded subtitle track.",
                            False,
                        )
                        created, embedded_reason = self._extract_embedded_subtitle(source, target)
                        if created:
                            self._emit_log(f"Created {target}. {embedded_reason}")

                    if not created and self.options.mode == MODE_EMBEDDED_ONLY:
                        raise RuntimeError(
                            "No extractable embedded text subtitle track was found. "
                            f"FFmpeg reported: {embedded_reason}"
                        )

                    if not created:
                        if embedded_reason:
                            self._emit_log(
                                f"Embedded subtitle extraction was unavailable for {source.name}; "
                                "falling back to speech transcription."
                            )
                        self._transcribe(source, target, file_index)
                        created = True
                        self._emit_log(f"Created transcribed SRT: {target}")

                    if created:
                        result["completed"] = int(result["completed"]) + 1
                        result["outputs"].append(str(target))
                        self.progress.emit(self._overall_progress(file_index, 1.0))

                except CancelledError:
                    raise
                except Exception as exc:
                    LOGGER.exception("Failed to process %s", source)
                    result["failed"] = int(result["failed"]) + 1
                    error = f"{source.name}: {exc}"
                    result["errors"].append(error)
                    self._emit_log(f"Failed: {error}")

        except CancelledError:
            result["cancelled"] = True
            self._emit_log("The operation was cancelled by the user.")
        except Exception as exc:
            LOGGER.exception("Unexpected worker failure")
            result["failed"] = int(result["failed"]) + 1
            result["errors"].append(f"Unexpected error: {exc}")
            self._emit_log(f"Unexpected error: {exc}")
        finally:
            self.finished.emit(result)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings("OpenAI", APP_SLUG)
        self.worker_thread: QThread | None = None
        self.worker: SubtitleWorker | None = None
        self.last_output_directories: list[Path] = []
        self.say_process: subprocess.Popen[bytes] | None = None
        self.close_when_finished = False

        self.setWindowTitle(APP_NAME)
        self.resize(820, 700)
        self.setMinimumSize(680, 560)
        self.setAcceptDrops(True)

        self._build_interface()
        self._build_menus_and_shortcuts()
        self._restore_settings()
        self._set_controls_running(False)
        self.file_list.setFocus()

    def _build_interface(self) -> None:
        central = QWidget(self)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(12)

        heading = QLabel("Create accessible SRT subtitle files")
        heading.setAccessibleName("Create accessible SRT subtitle files")
        heading.setStyleSheet("font-size: 20px; font-weight: 600;")
        outer.addWidget(heading)

        description = QLabel(
            "Add video or audio files. The app can extract an embedded text subtitle "
            "track or create subtitles from speech. Output is saved beside each source file."
        )
        description.setWordWrap(True)
        outer.addWidget(description)

        files_group = QGroupBox("1. Source files")
        files_layout = QVBoxLayout(files_group)

        file_label = QLabel("Selected video and audio files:")
        self.file_list = QListWidget()
        file_label.setBuddy(self.file_list)
        self.file_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.file_list.setAccessibleName("Selected video and audio files")
        self.file_list.setAccessibleDescription(
            "A list of files that will be processed. Use Command O to add files, "
            "and Delete or Backspace to remove selected files."
        )
        self.file_list.setMinimumHeight(150)
        files_layout.addWidget(file_label)
        files_layout.addWidget(self.file_list)

        file_buttons = QHBoxLayout()
        self.add_button = QPushButton("&Add files…")
        self.add_button.setAccessibleDescription(
            "Choose one or more video or audio files. The chooser opens in Downloads."
        )
        self.add_button.clicked.connect(self.select_files)
        file_buttons.addWidget(self.add_button)

        self.remove_button = QPushButton("&Remove selected")
        self.remove_button.clicked.connect(self.remove_selected_files)
        file_buttons.addWidget(self.remove_button)

        self.clear_button = QPushButton("C&lear list")
        self.clear_button.clicked.connect(self.clear_files)
        file_buttons.addWidget(self.clear_button)
        file_buttons.addStretch(1)
        files_layout.addLayout(file_buttons)
        outer.addWidget(files_group)

        options_group = QGroupBox("2. Subtitle options")
        options_layout = QFormLayout(options_group)
        options_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Prefer embedded subtitles; otherwise transcribe speech", MODE_PREFER)
        self.mode_combo.addItem("Always transcribe speech", MODE_TRANSCRIBE)
        self.mode_combo.addItem("Embedded text subtitles only", MODE_EMBEDDED_ONLY)
        self.mode_combo.setAccessibleName("Subtitle creation mode")
        self.mode_combo.setAccessibleDescription(
            "Choose whether to extract a subtitle track, transcribe speech, or try both automatically."
        )
        mode_label = QLabel("&Mode:")
        mode_label.setBuddy(self.mode_combo)
        options_layout.addRow(mode_label, self.mode_combo)

        self.model_combo = QComboBox()
        self.model_combo.addItem("Tiny — fastest, lowest accuracy", "tiny")
        self.model_combo.addItem("Base — fast", "base")
        self.model_combo.addItem("Small — balanced, recommended", "small")
        self.model_combo.addItem("Medium — more accurate", "medium")
        self.model_combo.addItem("Large v3 — highest accuracy, most demanding", "large-v3")
        self.model_combo.setAccessibleName("Speech recognition model")
        self.model_combo.setAccessibleDescription(
            "Larger models are usually more accurate and use more memory and processing."
        )
        model_label = QLabel("&Recognition model:")
        model_label.setBuddy(self.model_combo)
        options_layout.addRow(model_label, self.model_combo)

        self.language_combo = QComboBox()
        for label, code in (
            ("Automatic language detection", ""),
            ("Arabic", "ar"),
            ("English", "en"),
            ("French", "fr"),
            ("Spanish", "es"),
            ("German", "de"),
            ("Italian", "it"),
            ("Portuguese", "pt"),
            ("Dutch", "nl"),
            ("Turkish", "tr"),
            ("Chinese", "zh"),
            ("Japanese", "ja"),
        ):
            self.language_combo.addItem(label, code)
        self.language_combo.setAccessibleName("Spoken language")
        self.language_combo.setAccessibleDescription(
            "Automatic detection is recommended unless every selected file uses the same known language."
        )
        language_label = QLabel("&Spoken language:")
        language_label.setBuddy(self.language_combo)
        options_layout.addRow(language_label, self.language_combo)

        self.replace_checkbox = QCheckBox("Replace an existing non-empty SRT file")
        self.replace_checkbox.setAccessibleDescription(
            "Off by default. When off, existing subtitle files are safely skipped."
        )
        options_layout.addRow("Existing output:", self.replace_checkbox)

        self.speak_checkbox = QCheckBox("Speak major status updates using the macOS voice")
        self.speak_checkbox.setChecked(True)
        self.speak_checkbox.setAccessibleDescription(
            "Announces important progress and completion messages through the macOS say command."
        )
        options_layout.addRow("Announcements:", self.speak_checkbox)
        outer.addWidget(options_group)

        action_layout = QHBoxLayout()
        self.start_button = QPushButton("&Start SRT extraction")
        self.start_button.setDefault(True)
        self.start_button.setMinimumHeight(42)
        self.start_button.setAccessibleName("Start SRT extraction")
        self.start_button.setAccessibleDescription(
            "Begin processing every file in the selected files list. Shortcut: Command Return."
        )
        self.start_button.clicked.connect(self.start_extraction)
        action_layout.addWidget(self.start_button)

        self.cancel_button = QPushButton("&Cancel")
        self.cancel_button.setMinimumHeight(42)
        self.cancel_button.setAccessibleDescription(
            "Request cancellation. A subtitle file already completed remains safely saved."
        )
        self.cancel_button.clicked.connect(self.cancel_extraction)
        action_layout.addWidget(self.cancel_button)

        self.reveal_button = QPushButton("Reveal last &output")
        self.reveal_button.setMinimumHeight(42)
        self.reveal_button.clicked.connect(self.reveal_last_output)
        action_layout.addWidget(self.reveal_button)
        outer.addLayout(action_layout)

        progress_label = QLabel("Overall progress:")
        self.progress_bar = QProgressBar()
        progress_label.setBuddy(self.progress_bar)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setAccessibleName("Overall extraction progress")
        outer.addWidget(progress_label)
        outer.addWidget(self.progress_bar)

        self.status_label = QLabel("Ready. Add one or more files to begin.")
        self.status_label.setWordWrap(True)
        self.status_label.setAccessibleName("Current status")
        self.status_label.setAccessibleDescription(
            "The latest application status. Major changes can also be spoken aloud."
        )
        self.status_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        outer.addWidget(self.status_label)

        log_label = QLabel("Activity log:")
        self.log_view = QPlainTextEdit()
        log_label.setBuddy(self.log_view)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1000)
        self.log_view.setAccessibleName("Activity log")
        self.log_view.setAccessibleDescription(
            "Detailed processing messages. This field is read-only and can be reviewed with a screen reader."
        )
        self.log_view.setMinimumHeight(105)
        outer.addWidget(log_label)
        outer.addWidget(self.log_view)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar(self))
        self.statusBar().showMessage("Ready")

        QWidget.setTabOrder(self.file_list, self.add_button)
        QWidget.setTabOrder(self.add_button, self.remove_button)
        QWidget.setTabOrder(self.remove_button, self.clear_button)
        QWidget.setTabOrder(self.clear_button, self.mode_combo)
        QWidget.setTabOrder(self.mode_combo, self.model_combo)
        QWidget.setTabOrder(self.model_combo, self.language_combo)
        QWidget.setTabOrder(self.language_combo, self.replace_checkbox)
        QWidget.setTabOrder(self.replace_checkbox, self.speak_checkbox)
        QWidget.setTabOrder(self.speak_checkbox, self.start_button)
        QWidget.setTabOrder(self.start_button, self.cancel_button)
        QWidget.setTabOrder(self.cancel_button, self.reveal_button)
        QWidget.setTabOrder(self.reveal_button, self.log_view)

    def _build_menus_and_shortcuts(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        add_action = QAction("&Add files…", self)
        add_action.setShortcut(QKeySequence.Open)
        add_action.triggered.connect(self.select_files)
        file_menu.addAction(add_action)

        reveal_action = QAction("Reveal last &output", self)
        reveal_action.triggered.connect(self.reveal_last_output)
        file_menu.addAction(reveal_action)

        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        actions_menu = self.menuBar().addMenu("&Actions")
        start_action = QAction("&Start SRT extraction", self)
        start_action.setShortcut(QKeySequence("Meta+Return"))
        start_action.triggered.connect(self.start_extraction)
        actions_menu.addAction(start_action)

        cancel_action = QAction("&Cancel", self)
        cancel_action.setShortcut(QKeySequence("Escape"))
        cancel_action.triggered.connect(self.cancel_extraction)
        actions_menu.addAction(cancel_action)

        remove_action = QAction("Remove selected files", self)
        remove_action.setShortcuts([QKeySequence("Backspace"), QKeySequence("Delete")])
        remove_action.triggered.connect(self.remove_selected_files)
        actions_menu.addAction(remove_action)

        help_menu = self.menuBar().addMenu("&Help")
        accessibility_action = QAction("&Accessibility and keyboard help", self)
        accessibility_action.triggered.connect(self.show_accessibility_help)
        help_menu.addAction(accessibility_action)

        # Additional shortcuts work even when the corresponding menu is not open.
        self.clear_shortcut = QShortcut(QKeySequence("Meta+Shift+K"), self)
        self.clear_shortcut.activated.connect(self.clear_files)

    def _restore_settings(self) -> None:
        self._select_combo_data(self.mode_combo, self.settings.value("mode", MODE_PREFER))
        self._select_combo_data(self.model_combo, self.settings.value("model", "small"))
        self._select_combo_data(self.language_combo, self.settings.value("language", ""))
        self.replace_checkbox.setChecked(
            self.settings.value("replace", False, type=bool)
        )
        self.speak_checkbox.setChecked(self.settings.value("speak", True, type=bool))

    @staticmethod
    def _select_combo_data(combo: QComboBox, value: object) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _save_settings(self) -> None:
        self.settings.setValue("mode", self.mode_combo.currentData())
        self.settings.setValue("model", self.model_combo.currentData())
        self.settings.setValue("language", self.language_combo.currentData())
        self.settings.setValue("replace", self.replace_checkbox.isChecked())
        self.settings.setValue("speak", self.speak_checkbox.isChecked())

    def _announce(self, message: str) -> None:
        if not self.speak_checkbox.isChecked() or platform.system() != "Darwin":
            return
        try:
            if self.say_process is not None and self.say_process.poll() is None:
                self.say_process.terminate()
            self.say_process = subprocess.Popen(
                ["say", message],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

    def _set_status(self, message: str, announce: bool = False) -> None:
        self.status_label.setText(message)
        self.statusBar().showMessage(message)
        if announce:
            self._announce(message)

    def _append_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"{timestamp} — {message}")

    def _set_controls_running(self, running: bool) -> None:
        self.add_button.setEnabled(not running)
        self.remove_button.setEnabled(not running)
        self.clear_button.setEnabled(not running)
        self.mode_combo.setEnabled(not running)
        self.model_combo.setEnabled(not running)
        self.language_combo.setEnabled(not running)
        self.replace_checkbox.setEnabled(not running)
        self.start_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.reveal_button.setEnabled(bool(self.last_output_directories) and not running)
        self.file_list.setEnabled(not running)

    @Slot()
    def select_files(self) -> None:
        downloads = Path.home() / "Downloads"
        start_directory = downloads if downloads.exists() else Path.home()
        selected, _ = QFileDialog.getOpenFileNames(
            self,
            "Select video or audio files",
            str(start_directory),
            FILE_FILTER,
        )
        self.add_paths(Path(path) for path in selected)

    def add_paths(self, paths: Iterable[Path]) -> None:
        existing = {
            str(Path(self.file_list.item(index).data(Qt.UserRole)).resolve())
            for index in range(self.file_list.count())
        }
        added = 0
        rejected = 0

        for path in paths:
            resolved = path.expanduser().resolve()
            if (
                not resolved.is_file()
                or resolved.suffix.lower() not in SUPPORTED_SUFFIXES
                or str(resolved) in existing
            ):
                rejected += 1
                continue

            item = QListWidgetItem(resolved.name)
            item.setData(Qt.UserRole, str(resolved))
            item.setToolTip(str(resolved))
            item.setData(Qt.AccessibleDescriptionRole, str(resolved))
            self.file_list.addItem(item)
            existing.add(str(resolved))
            added += 1

        if added:
            self._set_status(f"Added {added} file{'s' if added != 1 else ''}.", announce=False)
            self._append_log(f"Added {added} source file{'s' if added != 1 else ''}.")
        elif rejected:
            self._set_status("No new supported files were added.", announce=True)

    @Slot()
    def remove_selected_files(self) -> None:
        if self.worker_thread is not None:
            return
        selected = self.file_list.selectedItems()
        for item in selected:
            self.file_list.takeItem(self.file_list.row(item))
        if selected:
            self._set_status(f"Removed {len(selected)} selected file{'s' if len(selected) != 1 else ''}.")

    @Slot()
    def clear_files(self) -> None:
        if self.worker_thread is not None:
            return
        count = self.file_list.count()
        self.file_list.clear()
        if count:
            self._set_status("Cleared the selected files list.")

    def _selected_paths(self) -> list[Path]:
        return [
            Path(self.file_list.item(index).data(Qt.UserRole))
            for index in range(self.file_list.count())
        ]

    @Slot()
    def start_extraction(self) -> None:
        if self.worker_thread is not None:
            return

        files = self._selected_paths()
        if not files:
            self._set_status("Add at least one video or audio file before starting.", announce=True)
            QMessageBox.information(
                self,
                APP_NAME,
                "Add at least one video or audio file before starting.",
            )
            self.add_button.setFocus()
            return

        self._save_settings()
        language_data = str(self.language_combo.currentData() or "").strip()
        options = JobOptions(
            model_name=str(self.model_combo.currentData()),
            language=language_data or None,
            mode=str(self.mode_combo.currentData()),
            replace_existing=self.replace_checkbox.isChecked(),
        )

        self.progress_bar.setValue(0)
        self.last_output_directories = []
        self._set_controls_running(True)
        self._set_status(
            f"Started processing {len(files)} file{'s' if len(files) != 1 else ''}.",
            announce=True,
        )
        self._append_log("Started SRT extraction.")

        self.worker_thread = QThread(self)
        self.worker = SubtitleWorker(files, options)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.status.connect(self._set_status)
        self.worker.log.connect(self._append_log)
        self.worker.finished.connect(self._worker_finished)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self._thread_finished)
        self.worker_thread.start()

    @Slot()
    def cancel_extraction(self) -> None:
        if self.worker is None:
            return
        self.worker.cancel()
        self.cancel_button.setEnabled(False)
        self._set_status(
            "Cancellation requested. The current safe stopping point will be used.",
            announce=True,
        )
        self._append_log("Cancellation requested.")

    @Slot(dict)
    def _worker_finished(self, result: dict) -> None:
        outputs = [Path(path) for path in result.get("outputs", [])]
        self.last_output_directories = list(dict.fromkeys(path.parent for path in outputs))
        self._set_controls_running(False)

        completed = int(result.get("completed", 0))
        skipped = int(result.get("skipped", 0))
        failed = int(result.get("failed", 0))
        cancelled = bool(result.get("cancelled", False))
        errors = [str(error) for error in result.get("errors", [])]

        if cancelled:
            summary = (
                f"Cancelled. Completed {completed}, skipped {skipped}, and failed {failed}."
            )
        else:
            self.progress_bar.setValue(100)
            summary = f"Finished. Completed {completed}, skipped {skipped}, and failed {failed}."

        self._set_status(summary, announce=True)
        self._append_log(summary)

        details = [summary]
        if outputs:
            details.append("\nCreated:\n" + "\n".join(str(path) for path in outputs))
        if errors:
            details.append("\nProblems:\n" + "\n".join(errors[:10]))
            if len(errors) > 10:
                details.append(f"\n{len(errors) - 10} additional errors are in the activity log.")
        details.append(f"\nDetailed log: {RUNTIME_LOG}")

        if failed:
            QMessageBox.warning(self, APP_NAME, "\n".join(details))
        else:
            QMessageBox.information(self, APP_NAME, "\n".join(details))

        if self.close_when_finished:
            self.close_when_finished = False
            self.close()
        else:
            self.start_button.setFocus()

    @Slot()
    def _thread_finished(self) -> None:
        thread = self.worker_thread
        self.worker = None
        self.worker_thread = None
        if thread is not None:
            thread.deleteLater()

    @Slot()
    def reveal_last_output(self) -> None:
        if not self.last_output_directories:
            self._set_status("No output folder is available yet.", announce=True)
            return
        for directory in self.last_output_directories:
            subprocess.Popen(
                ["open", str(directory)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    @Slot()
    def show_accessibility_help(self) -> None:
        QMessageBox.information(
            self,
            "Accessibility and keyboard help",
            "All controls use standard macOS-accessible Qt widgets and explicit labels.\n\n"
            "Command O: add files\n"
            "Command Return: start extraction\n"
            "Escape: request cancellation\n"
            "Delete or Backspace: remove selected files\n"
            "Command Shift K: clear the file list\n"
            "Command Q: quit\n\n"
            "Use Tab and Shift Tab to move between controls. The spoken-status checkbox "
            "uses the macOS voice for major messages. The activity log is read-only and "
            "can be reviewed with VoiceOver.",
        )

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        urls = event.mimeData().urls()
        if any(
            url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in SUPPORTED_SUFFIXES
            for url in urls
        ):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        self.add_paths(Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile())
        event.acceptProposedAction()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker_thread is not None and self.worker is not None:
            answer = QMessageBox.question(
                self,
                APP_NAME,
                "Extraction is still running. Request cancellation and close after the "
                "current safe stopping point?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer == QMessageBox.Yes:
                self.close_when_finished = True
                self.cancel_extraction()
            event.ignore()
            return

        self._save_settings()
        event.accept()


def main() -> int:
    if platform.system() != "Darwin":
        LOGGER.warning("The application was designed and tested conceptually for macOS.")

    application = QApplication(sys.argv)
    application.setApplicationName(APP_NAME)
    application.setOrganizationName("OpenAI")
    application.setOrganizationDomain("openai.com")

    window = MainWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        LOGGER.critical("Fatal application error\n%s", traceback.format_exc())
        native_error_dialog(
            f"A fatal error occurred. Details were written to {RUNTIME_LOG}."
        )
        raise
