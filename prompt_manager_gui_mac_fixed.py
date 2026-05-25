#!/usr/bin/env python3
"""
Prompt Manager GUI (macOS / Tkinter)

Refactored for clearer separation of concerns and improved accessibility:
- Higher-contrast buttons and controls
- Larger default fonts and spacing
- Clear focus indication for keyboard users
- Structured storage/service/UI layers in a single runnable file
- Same local storage location: ~/Library/Application Support/PromptManager/store.json
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import tkinter.font as tkfont


# ---------------------------------------------------------------------------
# App paths and constants
# ---------------------------------------------------------------------------

APP_NAME = "Prompt Manager"
SCRIPT_DIR = Path(__file__).resolve().parent
LEGACY_DATA_DIR = SCRIPT_DIR / "data"
LEGACY_STORE_PATH = LEGACY_DATA_DIR / "store.json"
LEGACY_DIRECT_STORE_PATH = SCRIPT_DIR / "store.json"
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "PromptManager"
STORE_PATH = APP_SUPPORT_DIR / "store.json"

SEPARATOR_STYLES: list[tuple[str, str]] = [
    ("Markdown rule (---)", "markdown_hr"),
    ("Headings (###)", "headings"),
    ("XML tags (<instruction>)", "xml_tags"),
]
SEPARATOR_LABEL_TO_KEY = {label: key for label, key in SEPARATOR_STYLES}
SEPARATOR_KEY_TO_LABEL = {key: label for label, key in SEPARATOR_STYLES}

ATTACHED_FILES_HINT = (
    "NOTE: The subject/input is provided via attached file(s). "
    "Please read and use the attachments as the source."
)


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class PromptRecord:
    id: str
    title: str
    content: str
    created_at: str
    updated_at: str

    @classmethod
    def from_raw(cls, raw: object) -> "PromptRecord | None":
        if not isinstance(raw, dict):
            return None
        created_at = str(raw.get("created_at") or utc_now_iso())
        updated_at = str(raw.get("updated_at") or created_at)
        return cls(
            id=str(raw.get("id") or uuid.uuid4()),
            title=str(raw.get("title") or "").strip(),
            content=str(raw.get("content") or "").rstrip(),
            created_at=created_at,
            updated_at=updated_at,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class AppStore:
    version: int = 1
    projects: dict[str, list[PromptRecord]] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "AppStore":
        return cls(version=1, projects={})

    @classmethod
    def from_raw(cls, raw: object) -> "AppStore":
        if not isinstance(raw, dict):
            raise ValueError("Store root must be a JSON object.")

        projects_raw = raw.get("projects")
        if not isinstance(projects_raw, dict):
            raise ValueError("Missing or invalid 'projects' object.")

        try:
            version = int(raw.get("version", 1))
        except Exception:
            version = 1

        projects: dict[str, list[PromptRecord]] = {}
        for raw_project_name, raw_prompts in projects_raw.items():
            project_name = normalize_project_name(str(raw_project_name))
            if not project_name or not isinstance(raw_prompts, list):
                continue
            cleaned: list[PromptRecord] = []
            for raw_prompt in raw_prompts:
                prompt = PromptRecord.from_raw(raw_prompt)
                if prompt is not None:
                    cleaned.append(prompt)
            projects[project_name] = cleaned

        return cls(version=version, projects=projects)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "projects": {
                project: [prompt.to_dict() for prompt in sorted(prompts, key=prompt_title_sort_key)]
                for project, prompts in sorted(
                    self.projects.items(),
                    key=lambda item: project_name_sort_key(item[0]),
                )
            },
        }

    def project_count(self) -> int:
        return len(self.projects)

    def prompt_count(self) -> int:
        return sum(len(prompts) for prompts in self.projects.values())


class StoreRepository:
    def __init__(self, active_store_path: Path = STORE_PATH) -> None:
        self.active_store_path = active_store_path
        self.app_support_dir = active_store_path.parent

    def ensure_data_dir(self) -> None:
        self.app_support_dir.mkdir(parents=True, exist_ok=True)

    def load_json_file(self, path: Path) -> AppStore:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        return AppStore.from_raw(raw)

    def store_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        for candidate in (LEGACY_STORE_PATH, LEGACY_DIRECT_STORE_PATH):
            if candidate.exists() and candidate != self.active_store_path:
                candidates.append(candidate)
        return candidates

    def backup_existing_store(self) -> Path | None:
        if not self.active_store_path.exists():
            return None
        backup_path = self.active_store_path.with_name(
            f"store.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        )
        shutil.copy2(self.active_store_path, backup_path)
        return backup_path

    def save(self, store: AppStore) -> None:
        self.ensure_data_dir()
        with self.active_store_path.open("w", encoding="utf-8") as handle:
            json.dump(store.to_dict(), handle, ensure_ascii=False, indent=2)

    def replace_from(self, source_path: Path) -> AppStore:
        store = self.load_json_file(source_path)
        if self.active_store_path.exists() and source_path.resolve() != self.active_store_path.resolve():
            self.backup_existing_store()
        self.save(store)
        return store

    def auto_import_best_candidate(self) -> bool:
        candidates: list[tuple[int, Path, AppStore]] = []
        for candidate in self.store_candidates():
            try:
                store = self.load_json_file(candidate)
            except Exception:
                continue
            candidates.append((store.prompt_count(), candidate, store))

        if not candidates:
            return False

        candidates.sort(key=lambda item: (item[0], str(item[1]).lower()), reverse=True)
        best_prompt_count, _best_path, best_store = candidates[0]
        if best_prompt_count <= 0:
            return False

        try:
            current = self.load_json_file(self.active_store_path) if self.active_store_path.exists() else None
        except Exception:
            current = None

        current_prompt_count = current.prompt_count() if current else -1
        if current_prompt_count >= best_prompt_count:
            return False

        self.save(best_store)
        return True

    def load(self) -> AppStore:
        self.ensure_data_dir()
        self.auto_import_best_candidate()

        if not self.active_store_path.exists():
            empty = AppStore.empty()
            self.save(empty)
            return empty

        return self.load_json_file(self.active_store_path)

    def safe_load(self) -> tuple[AppStore, str | None]:
        try:
            return self.load(), None
        except Exception as exc:
            backup_path = None
            try:
                backup_path = self.backup_existing_store()
            except Exception:
                backup_path = None
            empty = AppStore.empty()
            self.save(empty)
            message = f"Could not read:\n{self.active_store_path}\n\nReason: {exc}\n\nA fresh empty store was created."
            if backup_path:
                message += f"\n\nBackup saved to:\n{backup_path}"
            return empty, message


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


def normalize_project_name(name: str) -> str:
    return " ".join(name.strip().split())


def project_name_sort_key(name: str) -> tuple[str, str]:
    """Case-insensitive project sorting with deterministic tie-breaking."""
    normalized = normalize_project_name(name)
    return (normalized.casefold(), normalized)


def prompt_title_sort_key(prompt: PromptRecord) -> tuple[str, str, str, str]:
    """Case-insensitive prompt sorting by display title with deterministic tie-breaking."""
    display_title = prompt.title or "(untitled)"
    return (display_title.casefold(), display_title, prompt.created_at, prompt.id)


class PromptManagerService:
    def __init__(self, repository: StoreRepository) -> None:
        self.repository = repository
        self.store, self.load_warning = self.repository.safe_load()

    def reload(self) -> str | None:
        self.store, self.load_warning = self.repository.safe_load()
        return self.load_warning

    def save(self) -> None:
        self.repository.save(self.store)

    def list_projects(self) -> list[str]:
        return sorted(self.store.projects.keys(), key=project_name_sort_key)

    def get_prompts(self, project: str) -> list[PromptRecord]:
        return sorted(self.store.projects.get(project, []), key=prompt_title_sort_key)

    def find_prompt(self, project: str, prompt_id: str) -> PromptRecord | None:
        for prompt in self.store.projects.get(project, []):
            if prompt.id == prompt_id:
                return prompt
        return None

    def ensure_project(self, project_name: str) -> None:
        normalized = normalize_project_name(project_name)
        if normalized and normalized not in self.store.projects:
            self.store.projects[normalized] = []

    def add_project(self, project_name: str) -> str:
        project_name = normalize_project_name(project_name)
        if not project_name:
            raise ValueError("Please type a project name.")
        if project_name in self.store.projects:
            raise ValueError(f"Project '{project_name}' already exists.")
        self.store.projects[project_name] = []
        self.save()
        return project_name

    def delete_project(self, project_name: str) -> None:
        self.store.projects.pop(project_name, None)
        self.save()

    def upsert_prompt(
        self,
        project: str,
        title: str,
        content: str,
        prompt_id: str | None = None,
    ) -> str:
        project = normalize_project_name(project)
        if not project:
            raise ValueError("Please choose a valid destination project.")
        title = title.strip()
        content = content.rstrip()

        if not title:
            raise ValueError("Please enter a prompt title.")
        if not content:
            raise ValueError("Please paste or type the prompt content.")

        self.ensure_project(project)
        now = utc_now_iso()
        prompts = self.store.projects[project]

        if prompt_id:
            for prompt in prompts:
                if prompt.id == prompt_id:
                    prompt.title = title
                    prompt.content = content
                    prompt.updated_at = now
                    self.save()
                    return prompt.id

        new_prompt = PromptRecord(
            id=str(uuid.uuid4()),
            title=title,
            content=content,
            created_at=now,
            updated_at=now,
        )
        prompts.append(new_prompt)
        self.save()
        return new_prompt.id

    def move_prompt(
        self,
        src_project: str,
        dst_project: str,
        prompt_id: str,
        new_title: str,
        new_content: str,
    ) -> None:
        prompt = self.find_prompt(src_project, prompt_id)
        if prompt is None:
            raise ValueError("Prompt not found in source project.")

        dst_project = normalize_project_name(dst_project)
        if not dst_project:
            raise ValueError("Please choose a valid destination project.")

        self.ensure_project(dst_project)
        now = utc_now_iso()
        moved = PromptRecord(
            id=prompt.id,
            title=new_title.strip(),
            content=new_content.rstrip(),
            created_at=prompt.created_at,
            updated_at=now,
        )

        self.delete_prompt(src_project, prompt_id, save=False)
        self.store.projects[dst_project] = [
            existing for existing in self.store.projects[dst_project] if existing.id != prompt_id
        ]
        self.store.projects[dst_project].append(moved)
        self.save()

    def delete_prompt(self, project: str, prompt_id: str, save: bool = True) -> None:
        if project not in self.store.projects:
            return
        self.store.projects[project] = [
            prompt for prompt in self.store.projects[project] if prompt.id != prompt_id
        ]
        if save:
            self.save()

    def import_store_from_path(self, source_path: Path) -> AppStore:
        store = self.repository.replace_from(source_path)
        self.store = store
        return store


# ---------------------------------------------------------------------------
# Prompt composition helpers
# ---------------------------------------------------------------------------


def compose_final_input(prompt_text: str, subject_text: str, style_key: str) -> str:
    prompt_text = (prompt_text or "").rstrip()
    subject_text = (subject_text or "").rstrip()

    if style_key == "headings":
        return f"{prompt_text}\n\n### Subject\n{subject_text}\n"
    if style_key == "xml_tags":
        return (
            "<instruction>\n"
            f"{prompt_text}\n"
            "</instruction>\n\n"
            "<subject>\n"
            f"{subject_text}\n"
            "</subject>\n"
        )
    return f"{prompt_text}\n\n---\n\n{subject_text}\n"


def compose_prompt_with_attachments_hint(
    prompt_text: str,
    style_key: str,
    hint_text: str = ATTACHED_FILES_HINT,
) -> str:
    prompt_text = (prompt_text or "").rstrip()
    hint_text = (hint_text or "").rstrip()

    if style_key == "headings":
        return f"{prompt_text}\n\n### Attachments\n{hint_text}\n"
    if style_key == "xml_tags":
        return (
            "<instruction>\n"
            f"{prompt_text}\n"
            "</instruction>\n\n"
            "<attachments>\n"
            f"{hint_text}\n"
            "</attachments>\n"
        )
    return f"{prompt_text}\n\n[ATTACHED FILE(S)]\n{hint_text}\n"


VARIABLE_PATTERN = re.compile(r"{{\s*([^{}]+?)\s*}}")


def normalize_variable_name(name: str) -> str:
    """Normalize a template variable name while keeping it readable for display."""
    return " ".join(name.strip().split())


def extract_prompt_variables(prompt_text: str) -> list[str]:
    """Return unique {{VARIABLE}} names from one prompt, preserving first-seen order."""
    variables: list[str] = []
    seen: set[str] = set()
    for match in VARIABLE_PATTERN.finditer(prompt_text or ""):
        name = normalize_variable_name(match.group(1))
        if name and name not in seen:
            seen.add(name)
            variables.append(name)
    return variables


def extract_project_variables(prompts: list[PromptRecord]) -> list[str]:
    """Return unique {{VARIABLE}} names across a whole project/pipeline."""
    variables: list[str] = []
    seen: set[str] = set()
    for prompt in prompts:
        for name in extract_prompt_variables(prompt.content):
            if name not in seen:
                seen.add(name)
                variables.append(name)
    return variables


def render_prompt_template(prompt_text: str, variable_values: dict[str, str]) -> str:
    """Replace {{VARIABLE}} placeholders with user-provided values."""
    def replace_match(match: re.Match[str]) -> str:
        name = normalize_variable_name(match.group(1))
        return variable_values.get(name, match.group(0))

    return VARIABLE_PATTERN.sub(replace_match, prompt_text or "")


# ---------------------------------------------------------------------------
# Theme and reusable widgets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Palette:
    window_bg: str = "#F6F7FB"
    card_bg: str = "#FFFFFF"
    card_alt_bg: str = "#F0F4FF"
    field_bg: str = "#FFFFFF"
    text: str = "#111827"
    muted: str = "#374151"
    border: str = "#6B7280"
    accent: str = "#0F62FE"
    accent_hover: str = "#0353E9"
    accent_pressed: str = "#002D9C"
    accent_text: str = "#111827"
    neutral: str = "#E5E7EB"
    neutral_hover: str = "#D1D5DB"
    neutral_pressed: str = "#9CA3AF"
    neutral_text: str = "#111827"
    danger: str = "#B42318"
    danger_hover: str = "#912018"
    danger_pressed: str = "#7A1212"
    danger_text: str = "#111827"
    focus: str = "#111827"
    selection: str = "#C7D2FE"
    status_bg: str = "#E0E7FF"


@dataclass(frozen=True)
class FontSet:
    sans: str
    mono: str
    base: tuple[str, int]
    small: tuple[str, int]
    heading: tuple[str, int, str]
    section: tuple[str, int, str]
    mono_base: tuple[str, int]
    button: tuple[str, int, str]


class ThemeManager:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.palette = Palette()
        self.fonts = self._build_fonts()
        self._configure_root()
        self._configure_ttk()

    def _pick_first_font(self, candidates: tuple[str, ...], fallback: str) -> str:
        try:
            families = {family.lower(): family for family in tkfont.families(self.root)}
        except Exception:
            return fallback
        for candidate in candidates:
            match = families.get(candidate.lower())
            if match:
                return match
        return fallback

    def _build_fonts(self) -> FontSet:
        sans = self._pick_first_font(
            ("SF Pro Text", "Helvetica Neue", "Arial", "Verdana"),
            "TkDefaultFont",
        )
        mono = self._pick_first_font(("SF Mono", "Menlo", "Monaco", "Courier New"), "TkFixedFont")

        default_font = tkfont.nametofont("TkDefaultFont")
        default_font.configure(family=sans, size=14)
        text_font = (sans, 14)
        small_font = (sans, 12)
        heading_font = (sans, 22, "bold")
        section_font = (sans, 15, "bold")
        mono_font = (mono, 13)
        button_font = (sans, 14, "bold")
        return FontSet(
            sans=sans,
            mono=mono,
            base=text_font,
            small=small_font,
            heading=heading_font,
            section=section_font,
            mono_base=mono_font,
            button=button_font,
        )

    def _configure_root(self) -> None:
        self.root.configure(bg=self.palette.window_bg)
        self.root.option_add("*tearOff", False)
        self.root.option_add("*Listbox.font", self.fonts.base)
        self.root.option_add("*Text.font", self.fonts.base)
        self.root.option_add("*Text.selectBackground", self.palette.selection)
        self.root.option_add("*Text.selectForeground", self.palette.text)

    def _configure_ttk(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("aqua")
        except tk.TclError:
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass

        style.configure("TFrame", background=self.palette.window_bg)
        style.configure("Card.TFrame", background=self.palette.card_bg)
        style.configure(
            "TLabel",
            background=self.palette.window_bg,
            foreground=self.palette.text,
            font=self.fonts.base,
        )
        style.configure(
            "Card.TLabel",
            background=self.palette.card_bg,
            foreground=self.palette.text,
            font=self.fonts.base,
        )
        style.configure(
            "Header.TLabel",
            background=self.palette.window_bg,
            foreground=self.palette.text,
            font=self.fonts.heading,
        )
        style.configure(
            "Section.TLabel",
            background=self.palette.card_bg,
            foreground=self.palette.text,
            font=self.fonts.section,
        )
        style.configure(
            "Muted.TLabel",
            background=self.palette.window_bg,
            foreground=self.palette.muted,
            font=self.fonts.small,
        )
        style.configure(
            "Muted.Card.TLabel",
            background=self.palette.card_bg,
            foreground=self.palette.muted,
            font=self.fonts.small,
        )
        style.configure("Accessible.TCombobox", padding=8)
        style.configure("Accessible.TEntry", padding=8)


class Card(tk.Frame):
    def __init__(self, master: tk.Misc, theme: ThemeManager, **kwargs) -> None:
        super().__init__(
            master,
            bg=theme.palette.card_bg,
            highlightbackground=theme.palette.border,
            highlightcolor=theme.palette.focus,
            highlightthickness=1,
            bd=0,
            **kwargs,
        )


class AccessibleButton(tk.Button):
    def __init__(
        self,
        master: tk.Misc,
        theme: ThemeManager,
        *,
        text: str,
        command: Callable[[], None],
        kind: str = "accent",
        width: int | None = None,
    ) -> None:
        palette = theme.palette

        normal_bg = palette.accent
        hover_bg = palette.accent_hover
        pressed_bg = palette.accent_pressed
        fg = palette.accent_text

        if kind == "neutral":
            normal_bg = palette.neutral
            hover_bg = palette.neutral_hover
            pressed_bg = palette.neutral_pressed
            fg = palette.neutral_text
        elif kind == "danger":
            normal_bg = palette.danger
            hover_bg = palette.danger_hover
            pressed_bg = palette.danger_pressed
            fg = palette.danger_text

        self._normal_bg = normal_bg
        self._hover_bg = hover_bg
        self._pressed_bg = pressed_bg

        super().__init__(
            master,
            text=text,
            command=command,
            font=theme.fonts.button,
            bg=normal_bg,
            fg=fg,
            activebackground=pressed_bg,
            activeforeground=fg,
            disabledforeground=palette.muted,
            bd=1,
            relief="solid",
            padx=16,
            pady=10,
            cursor="hand2",
            width=width or 0,
            takefocus=True,
            highlightthickness=3,
            highlightbackground=normal_bg,
            highlightcolor=palette.focus,
            anchor="center",
        )
        self.bind("<Enter>", lambda _event: self.configure(bg=self._hover_bg))
        self.bind("<Leave>", lambda _event: self.configure(bg=self._normal_bg))
        self.bind("<ButtonPress-1>", lambda _event: self.configure(bg=self._pressed_bg))
        self.bind("<ButtonRelease-1>", lambda _event: self.configure(bg=self._hover_bg))
        self.bind("<FocusIn>", lambda _event: self.configure(highlightbackground=palette.focus))
        self.bind("<FocusOut>", lambda _event: self.configure(highlightbackground=self._normal_bg))
        self.bind("<Return>", self._invoke_from_keyboard)
        self.bind("<KP_Enter>", self._invoke_from_keyboard)

    def _invoke_from_keyboard(self, _event=None) -> str:
        self.invoke()
        return "break"

class AccessibleText(tk.Text):
    def __init__(self, master: tk.Misc, theme: ThemeManager, **kwargs) -> None:
        super().__init__(
            master,
            bg=theme.palette.field_bg,
            fg=theme.palette.text,
            insertbackground=theme.palette.text,
            selectbackground=theme.palette.selection,
            selectforeground=theme.palette.text,
            relief="solid",
            bd=1,
            highlightthickness=2,
            highlightbackground=theme.palette.border,
            highlightcolor=theme.palette.focus,
            wrap="word",
            font=theme.fonts.base,
            undo=True,
            padx=10,
            pady=10,
            **kwargs,
        )


class AccessibleEntry(tk.Entry):
    def __init__(self, master: tk.Misc, theme: ThemeManager, textvariable: tk.StringVar, **kwargs) -> None:
        super().__init__(
            master,
            textvariable=textvariable,
            bg=theme.palette.field_bg,
            fg=theme.palette.text,
            insertbackground=theme.palette.text,
            relief="solid",
            bd=1,
            highlightthickness=2,
            highlightbackground=theme.palette.border,
            highlightcolor=theme.palette.focus,
            font=theme.fonts.base,
            **kwargs,
        )


class TextAreaWithScrollbar(tk.Frame):
    """A keyboard-focusable multi-line field with a persistent scrollbar."""

    def __init__(
        self,
        master: tk.Misc,
        theme: ThemeManager,
        *,
        height: int = 8,
        read_only: bool = False,
    ) -> None:
        super().__init__(master, bg=theme.palette.card_bg)
        self.read_only = read_only
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.text = AccessibleText(self, theme, height=height)
        self.text.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=self.scrollbar.set)
        if read_only:
            self.text.config(state="disabled")

    def set_text(self, content: str) -> None:
        was_disabled = str(self.text.cget("state")) == "disabled"
        if was_disabled:
            self.text.config(state="normal")
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", content or "")
        if was_disabled or self.read_only:
            self.text.config(state="disabled")

    def get_text(self) -> str:
        return self.text.get("1.0", tk.END).rstrip()

    def clear(self) -> None:
        self.set_text("")

    def focus_set(self) -> None:
        self.text.focus_set()


# ---------------------------------------------------------------------------
# UI pages
# ---------------------------------------------------------------------------


class BasePage(ttk.Frame):
    def __init__(self, parent: tk.Misc, app: "PromptManagerApp") -> None:
        super().__init__(parent)
        self.app = app
        self.theme = app.theme
        self.service = app.service

    def on_show(self) -> None:
        pass

    def refresh(self) -> None:
        pass


class HomePage(BasePage):
    def __init__(self, parent: tk.Misc, app: "PromptManagerApp") -> None:
        super().__init__(parent, app)

        ttk.Label(self, text=APP_NAME, style="Header.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Label(
            self,
            text=(
                "Manage Projects and Prompts locally, then generate final inputs with a temporary subject "
                "or fill reusable pipeline variables. This version uses larger text and higher contrast."
            ),
            style="Muted.TLabel",
            wraplength=940,
            justify="left",
        ).pack(anchor="w", pady=(0, 18))

        quick_actions = Card(self, self.theme)
        quick_actions.pack(fill="x", pady=(0, 14))
        inner = tk.Frame(quick_actions, bg=self.theme.palette.card_bg)
        inner.pack(fill="x", padx=18, pady=18)

        AccessibleButton(inner, self.theme, text="1) Manage Projects", command=lambda: app.show("ManageProjectsPage"), width=28).grid(row=0, column=0, sticky="w", padx=6, pady=8)
        AccessibleButton(inner, self.theme, text="2) Manage Prompts", command=lambda: app.show("ManagePromptsPage"), width=28).grid(row=1, column=0, sticky="w", padx=6, pady=8)
        AccessibleButton(inner, self.theme, text="3) Handle a Subject", command=lambda: app.show("HandleSubjectPage"), width=28).grid(row=2, column=0, sticky="w", padx=6, pady=8)
        AccessibleButton(inner, self.theme, text="4) Handle a Pipeline", command=lambda: app.show("HandlePipelinePage"), width=28).grid(row=3, column=0, sticky="w", padx=6, pady=8)

        info_card = Card(self, self.theme)
        info_card.pack(fill="x")
        info_inner = tk.Frame(info_card, bg=self.theme.palette.card_bg)
        info_inner.pack(fill="x", padx=18, pady=18)

        ttk.Label(info_inner, text="Storage and import", style="Section.TLabel").pack(anchor="w")
        self.store_status_label = ttk.Label(info_inner, text="", style="Muted.Card.TLabel", wraplength=920, justify="left")
        self.store_status_label.pack(anchor="w", pady=(10, 4))
        self.store_hint_label = ttk.Label(info_inner, text="", style="Muted.Card.TLabel", wraplength=920, justify="left")
        self.store_hint_label.pack(anchor="w")

        buttons = tk.Frame(info_inner, bg=self.theme.palette.card_bg)
        buttons.pack(anchor="w", pady=(14, 0))
        AccessibleButton(buttons, self.theme, text="Open storage folder", command=self.app.open_storage_folder, kind="neutral").pack(side="left")
        AccessibleButton(buttons, self.theme, text="Import store.json…", command=self.app.import_store_dialog, kind="neutral").pack(side="left", padx=(12, 0))

        self.refresh()

    def refresh(self) -> None:
        self.store_status_label.config(text=f"Active store: {self.app.repository.active_store_path}")
        self.store_hint_label.config(
            text=(
                f"Loaded {self.service.store.project_count()} project(s) and {self.service.store.prompt_count()} prompt(s). "
                "If your old Ubuntu file is elsewhere, use 'Import store.json…'."
            )
        )


class ManageProjectsPage(BasePage):
    def __init__(self, parent: tk.Misc, app: "PromptManagerApp") -> None:
        super().__init__(parent, app)

        ttk.Label(self, text="Manage Projects", style="Header.TLabel").pack(anchor="w")
        ttk.Label(self, text="Projects are stored in Application Support on this Mac.", style="Muted.TLabel").pack(anchor="w", pady=(4, 14))

        nav = tk.Frame(self, bg=self.theme.palette.window_bg)
        nav.pack(fill="x", pady=(0, 12))
        AccessibleButton(nav, self.theme, text="← Home", command=lambda: app.show("HomePage"), kind="neutral").pack(side="left")

        form = Card(self, self.theme)
        form.pack(fill="x", pady=(0, 14))
        form_inner = tk.Frame(form, bg=self.theme.palette.card_bg)
        form_inner.pack(fill="x", padx=18, pady=18)

        ttk.Label(form_inner, text="Project name", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.project_name_var = tk.StringVar()
        self.project_entry = AccessibleEntry(form_inner, self.theme, self.project_name_var, width=42)
        self.project_entry.grid(row=1, column=0, sticky="w", pady=(8, 0))
        AccessibleButton(form_inner, self.theme, text="Add Project", command=self.add_project).grid(row=1, column=1, padx=12, sticky="w")

        list_card = Card(self, self.theme)
        list_card.pack(fill="both", expand=True)
        list_inner = tk.Frame(list_card, bg=self.theme.palette.card_bg)
        list_inner.pack(fill="both", expand=True, padx=18, pady=18)

        ttk.Label(list_inner, text="Existing projects", style="Section.TLabel").pack(anchor="w")
        self.projects_list = tk.Listbox(
            list_inner,
            bg=self.theme.palette.field_bg,
            fg=self.theme.palette.text,
            selectbackground=self.theme.palette.selection,
            selectforeground=self.theme.palette.text,
            relief="solid",
            bd=1,
            highlightthickness=2,
            highlightbackground=self.theme.palette.border,
            highlightcolor=self.theme.palette.focus,
            font=self.theme.fonts.base,
            height=14,
        )
        self.projects_list.pack(fill="both", expand=True, pady=(10, 12))

        actions = tk.Frame(list_inner, bg=self.theme.palette.card_bg)
        actions.pack(fill="x")
        AccessibleButton(actions, self.theme, text="Delete selected project", command=self.delete_selected_project, kind="danger").pack(side="left")
        ttk.Label(actions, text="Deletes all prompts inside it.", style="Muted.Card.TLabel").pack(side="left", padx=12)

        self.refresh()

    def on_show(self) -> None:
        self.project_entry.focus_set()

    def refresh(self) -> None:
        self.projects_list.delete(0, tk.END)
        for name in self.service.list_projects():
            self.projects_list.insert(tk.END, name)

    def add_project(self) -> None:
        try:
            created = self.service.add_project(self.project_name_var.get())
        except ValueError as exc:
            messagebox.showwarning("Project", str(exc), parent=self.app)
            return
        self.project_name_var.set("")
        self.app.refresh_all_pages()
        messagebox.showinfo("Saved", f"Project '{created}' added.", parent=self.app)

    def delete_selected_project(self) -> None:
        selected = self.projects_list.curselection()
        if not selected:
            messagebox.showwarning("Select a project", "Please select a project to delete.", parent=self.app)
            return
        project = self.projects_list.get(selected[0])
        if not messagebox.askyesno(
            "Confirm delete",
            f"Delete project '{project}' and all its prompts?",
            parent=self.app,
        ):
            return
        self.service.delete_project(project)
        self.app.refresh_all_pages()
        messagebox.showinfo("Deleted", f"Project '{project}' deleted.", parent=self.app)


class ManagePromptsPage(BasePage):
    def __init__(self, parent: tk.Misc, app: "PromptManagerApp") -> None:
        super().__init__(parent, app)
        self.current_prompt_id: str | None = None
        self.original_project: str | None = None

        ttk.Label(self, text="Manage Prompts", style="Header.TLabel").pack(anchor="w")
        ttk.Label(self, text="Add, edit, delete, or move prompts between projects.", style="Muted.TLabel").pack(anchor="w", pady=(4, 14))

        nav = tk.Frame(self, bg=self.theme.palette.window_bg)
        nav.pack(fill="x", pady=(0, 12))
        AccessibleButton(nav, self.theme, text="← Home", command=lambda: app.show("HomePage"), kind="neutral").pack(side="left")

        main = tk.Frame(self, bg=self.theme.palette.window_bg)
        main.pack(fill="both", expand=True)

        left = Card(main, self.theme)
        right = Card(main, self.theme)
        left.pack(side="left", fill="both", expand=True, padx=(0, 10))
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        lf = tk.Frame(left, bg=self.theme.palette.card_bg)
        lf.pack(fill="both", expand=True, padx=18, pady=18)

        ttk.Label(lf, text="Editing status", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.editing_label = ttk.Label(lf, text="New prompt", style="Muted.Card.TLabel")
        self.editing_label.grid(row=0, column=1, sticky="w", padx=(12, 0))

        ttk.Label(lf, text="Destination project", style="Section.TLabel").grid(row=1, column=0, sticky="w", pady=(14, 0))
        self.dest_project_var = tk.StringVar()
        self.dest_project_combo = ttk.Combobox(lf, textvariable=self.dest_project_var, state="readonly", width=30, style="Accessible.TCombobox")
        self.dest_project_combo.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 12))

        ttk.Label(lf, text="Prompt title", style="Section.TLabel").grid(row=3, column=0, sticky="w")
        self.title_var = tk.StringVar()
        self.title_entry = AccessibleEntry(lf, self.theme, self.title_var, width=42)
        self.title_entry.grid(row=4, column=0, columnspan=2, sticky="we", pady=(8, 12))

        ttk.Label(lf, text="Prompt content", style="Section.TLabel").grid(row=5, column=0, sticky="w")
        self.content_text = AccessibleText(lf, self.theme, height=16)
        self.content_text.grid(row=6, column=0, columnspan=2, sticky="nsew", pady=(8, 12))

        lf.grid_columnconfigure(0, weight=1)
        lf.grid_rowconfigure(6, weight=1)

        buttons = tk.Frame(lf, bg=self.theme.palette.card_bg)
        buttons.grid(row=7, column=0, columnspan=2, sticky="we")
        AccessibleButton(buttons, self.theme, text="Save (add/update/move)", command=self.save_prompt).pack(side="left")
        AccessibleButton(buttons, self.theme, text="Clear (new prompt)", command=self.clear_form, kind="neutral").pack(side="left", padx=12)
        AccessibleButton(buttons, self.theme, text="Delete current prompt", command=self.delete_current_prompt, kind="danger").pack(side="right")

        self.status = ttk.Label(lf, text="", style="Muted.Card.TLabel", wraplength=500, justify="left")
        self.status.grid(row=8, column=0, columnspan=2, sticky="w", pady=(12, 0))

        rf = tk.Frame(right, bg=self.theme.palette.card_bg)
        rf.pack(fill="both", expand=True, padx=18, pady=18)

        ttk.Label(rf, text="Browse prompts by project", style="Section.TLabel").pack(anchor="w")
        self.filter_project_var = tk.StringVar()
        self.filter_project_combo = ttk.Combobox(rf, textvariable=self.filter_project_var, state="readonly", width=36, style="Accessible.TCombobox")
        self.filter_project_combo.pack(anchor="w", pady=(8, 12))
        self.filter_project_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_prompt_list())

        ttk.Label(rf, text="Prompts in selected project", style="Section.TLabel").pack(anchor="w")
        self.prompts_list = tk.Listbox(
            rf,
            bg=self.theme.palette.field_bg,
            fg=self.theme.palette.text,
            selectbackground=self.theme.palette.selection,
            selectforeground=self.theme.palette.text,
            relief="solid",
            bd=1,
            highlightthickness=2,
            highlightbackground=self.theme.palette.border,
            highlightcolor=self.theme.palette.focus,
            font=self.theme.fonts.base,
            height=18,
        )
        self.prompts_list.pack(fill="both", expand=True, pady=(10, 12))
        self.prompts_list.bind("<<ListboxSelect>>", lambda _e: self.load_selected_prompt())

        ttk.Label(
            rf,
            text="Select a prompt to edit it, then change the destination project if you want to move it.",
            style="Muted.Card.TLabel",
            wraplength=380,
            justify="left",
        ).pack(anchor="w")

        self.refresh()

    def on_show(self) -> None:
        if not self.service.list_projects():
            messagebox.showinfo("No projects yet", "Please add a project first.", parent=self.app)
            self.app.show("ManageProjectsPage")
            return
        self.title_entry.focus_set()

    def refresh(self) -> None:
        projects = self.service.list_projects()
        self.dest_project_combo["values"] = projects
        self.filter_project_combo["values"] = projects
        if projects:
            if self.filter_project_var.get() not in projects:
                self.filter_project_var.set(projects[0])
            if self.dest_project_var.get() not in projects:
                self.dest_project_var.set(projects[0])
        self.refresh_prompt_list()

    def refresh_prompt_list(self) -> None:
        self.prompts_list.delete(0, tk.END)
        project = self.filter_project_var.get()
        if not project:
            self.status.config(text="No project selected for browsing.")
            return
        prompts = self.service.get_prompts(project)
        for prompt in prompts:
            short_id = prompt.id[:8] if prompt.id else "--------"
            title = prompt.title or "(untitled)"
            self.prompts_list.insert(tk.END, f"{title}  [{short_id}]")
        self.status.config(text=f"Browsing '{project}': {len(prompts)} prompt(s).")

    def clear_form(self) -> None:
        self.current_prompt_id = None
        self.original_project = None
        self.editing_label.config(text="New prompt")
        self.title_var.set("")
        self.content_text.delete("1.0", tk.END)
        projects = self.service.list_projects()
        if projects and self.dest_project_var.get() not in projects:
            self.dest_project_var.set(projects[0])
        self.status.config(text="Cleared form (new prompt).")

    def _get_selected_prompt_id(self) -> str | None:
        project = self.filter_project_var.get()
        selection = self.prompts_list.curselection()
        if not project or not selection:
            return None
        prompts = self.service.get_prompts(project)
        idx = selection[0]
        if idx < 0 or idx >= len(prompts):
            return None
        return prompts[idx].id

    def load_selected_prompt(self) -> None:
        project = self.filter_project_var.get()
        prompt_id = self._get_selected_prompt_id()
        if not project or not prompt_id:
            return
        prompt = self.service.find_prompt(project, prompt_id)
        if prompt is None:
            return

        self.current_prompt_id = prompt.id
        self.original_project = project
        self.editing_label.config(text=f"Editing [{prompt.id[:8]}] from '{project}'")
        self.dest_project_var.set(project)
        self.title_var.set(prompt.title)
        self.content_text.delete("1.0", tk.END)
        self.content_text.insert("1.0", prompt.content)

    def save_prompt(self) -> None:
        projects = self.service.list_projects()
        if not projects:
            messagebox.showwarning("No projects", "Please add a project first.", parent=self.app)
            return

        dest_project = self.dest_project_var.get()
        title = self.title_var.get().strip()
        content = self.content_text.get("1.0", tk.END).rstrip()

        try:
            if not self.current_prompt_id:
                prompt_id = self.service.upsert_prompt(dest_project, title, content)
                self.current_prompt_id = prompt_id
                self.original_project = dest_project
                self.editing_label.config(text=f"Editing [{prompt_id[:8]}] from '{dest_project}'")
                self.app.refresh_all_pages()
                self.status.config(text=f"Created prompt [{prompt_id[:8]}] in '{dest_project}'.")
                messagebox.showinfo("Saved", "Prompt created.", parent=self.app)
                return

            prompt_id = self.current_prompt_id
            src_project = self.original_project or dest_project

            if src_project == dest_project:
                self.service.upsert_prompt(dest_project, title, content, prompt_id=prompt_id)
                self.app.refresh_all_pages()
                self.status.config(text=f"Updated prompt [{prompt_id[:8]}] in '{dest_project}'.")
                messagebox.showinfo("Saved", "Prompt updated.", parent=self.app)
                return

            self.service.move_prompt(src_project, dest_project, prompt_id, title, content)
            self.original_project = dest_project
            self.filter_project_var.set(dest_project)
            self.app.refresh_all_pages()
            self.editing_label.config(text=f"Editing [{prompt_id[:8]}] from '{dest_project}'")
            self.status.config(text=f"Moved prompt [{prompt_id[:8]}] from '{src_project}' → '{dest_project}'.")
            messagebox.showinfo("Saved", "Prompt moved successfully.", parent=self.app)
        except ValueError as exc:
            messagebox.showwarning("Prompt", str(exc), parent=self.app)

    def delete_current_prompt(self) -> None:
        if not self.current_prompt_id or not self.original_project:
            messagebox.showwarning("No prompt selected", "Select a prompt from the list first.", parent=self.app)
            return
        prompt_id = self.current_prompt_id
        project = self.original_project
        if not messagebox.askyesno(
            "Confirm delete",
            f"Delete prompt [{prompt_id[:8]}] from '{project}'?",
            parent=self.app,
        ):
            return
        self.service.delete_prompt(project, prompt_id)
        self.app.refresh_all_pages()
        self.clear_form()
        messagebox.showinfo("Deleted", "Prompt deleted.", parent=self.app)


class HandleSubjectPage(BasePage):
    def __init__(self, parent: tk.Misc, app: "PromptManagerApp") -> None:
        super().__init__(parent, app)

        ttk.Label(self, text="Handle Subject", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            self,
            text="One-off mode: pick one prompt, add a temporary subject, then copy the final input.",
            style="Muted.TLabel",
            wraplength=940,
            justify="left",
        ).pack(anchor="w", pady=(4, 14))

        nav = tk.Frame(self, bg=self.theme.palette.window_bg)
        nav.pack(fill="x", pady=(0, 12))
        AccessibleButton(nav, self.theme, text="← Home", command=lambda: app.show("HomePage"), kind="neutral").pack(side="left")
        AccessibleButton(nav, self.theme, text="Handle Pipeline", command=lambda: app.show("HandlePipelinePage"), kind="neutral").pack(side="left", padx=(12, 0))

        main = tk.Frame(self, bg=self.theme.palette.window_bg)
        main.pack(fill="both", expand=True)

        left = Card(main, self.theme)
        right = Card(main, self.theme)
        left.pack(side="left", fill="both", expand=True, padx=(0, 10))
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        lf = tk.Frame(left, bg=self.theme.palette.card_bg)
        lf.pack(fill="both", expand=True, padx=18, pady=18)
        lf.grid_columnconfigure(0, weight=1)
        lf.grid_columnconfigure(1, weight=1)
        lf.grid_columnconfigure(2, weight=1)
        lf.grid_rowconfigure(1, weight=1)

        ttk.Label(lf, text="Subject (temporary)", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        self.subject_area = TextAreaWithScrollbar(lf, self.theme, height=14)
        self.subject_area.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=(8, 12))

        ttk.Label(lf, text="Project", style="Section.TLabel").grid(row=2, column=0, sticky="w")
        self.project_var = tk.StringVar()
        self.project_combo = ttk.Combobox(lf, textvariable=self.project_var, state="readonly", width=24, style="Accessible.TCombobox")
        self.project_combo.grid(row=3, column=0, sticky="we", pady=(8, 12), padx=(0, 8))
        self.project_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_prompts_for_project())

        ttk.Label(lf, text="Prompt", style="Section.TLabel").grid(row=2, column=1, sticky="w")
        self.prompt_var = tk.StringVar()
        self.prompt_combo = ttk.Combobox(lf, textvariable=self.prompt_var, state="readonly", width=30, style="Accessible.TCombobox")
        self.prompt_combo.grid(row=3, column=1, sticky="we", pady=(8, 12), padx=(0, 8))
        self.prompt_combo.bind("<<ComboboxSelected>>", lambda _e: self.preview_selected_prompt())

        ttk.Label(lf, text="Separator style", style="Section.TLabel").grid(row=2, column=2, sticky="w")
        self.sep_var = tk.StringVar(value="markdown_hr")
        self.sep_combo = ttk.Combobox(
            lf,
            state="readonly",
            values=[label for label, _ in SEPARATOR_STYLES],
            width=24,
            style="Accessible.TCombobox",
        )
        self.sep_combo.grid(row=3, column=2, sticky="we", pady=(8, 12))
        self.sep_combo.set(SEPARATOR_KEY_TO_LABEL[self.sep_var.get()])
        self.sep_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_sep_changed())

        buttons = tk.Frame(lf, bg=self.theme.palette.card_bg)
        buttons.grid(row=4, column=0, columnspan=3, sticky="we")
        AccessibleButton(buttons, self.theme, text="Generate final input", command=self.generate).pack(side="left")
        AccessibleButton(buttons, self.theme, text="Clear subject", command=self.clear_subject, kind="neutral").pack(side="left", padx=12)

        self.hint = ttk.Label(lf, text="", style="Muted.Card.TLabel", wraplength=620, justify="left")
        self.hint.grid(row=5, column=0, columnspan=3, sticky="w", pady=(12, 0))

        rf = tk.Frame(right, bg=self.theme.palette.card_bg)
        rf.pack(fill="both", expand=True, padx=18, pady=18)
        rf.grid_columnconfigure(0, weight=1)
        rf.grid_rowconfigure(1, weight=1)
        rf.grid_rowconfigure(4, weight=2)

        ttk.Label(rf, text="Prompt preview", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.prompt_preview_area = TextAreaWithScrollbar(rf, self.theme, height=8, read_only=True)
        self.prompt_preview_area.grid(row=1, column=0, sticky="nsew", pady=(8, 14))

        ttk.Label(rf, text="Final input (copy this into an LLM)", style="Section.TLabel").grid(row=2, column=0, sticky="w")
        output_buttons = tk.Frame(rf, bg=self.theme.palette.card_bg)
        output_buttons.grid(row=3, column=0, sticky="w", pady=(8, 8))
        AccessibleButton(output_buttons, self.theme, text="Copy final input", command=self.copy_final).pack(side="left")
        AccessibleButton(output_buttons, self.theme, text="Clear final input", command=self.clear_final, kind="neutral").pack(side="left", padx=12)

        self.final_area = TextAreaWithScrollbar(rf, self.theme, height=12)
        self.final_area.grid(row=4, column=0, sticky="nsew")

        self.refresh()

    def on_show(self) -> None:
        if not self.service.list_projects():
            messagebox.showinfo(
                "No projects yet",
                "Please add a project and a prompt first.",
                parent=self.app,
            )
            self.app.show("ManageProjectsPage")
            return
        self.subject_area.focus_set()

    def refresh(self) -> None:
        projects = self.service.list_projects()
        self.project_combo["values"] = projects
        if projects and self.project_var.get() not in projects:
            self.project_var.set(projects[0])
        self.refresh_prompts_for_project()

    def _on_sep_changed(self) -> None:
        label = self.sep_combo.get()
        self.sep_var.set(SEPARATOR_LABEL_TO_KEY.get(label, "markdown_hr"))

    def refresh_prompts_for_project(self) -> None:
        project = self.project_var.get()
        prompts = self.service.get_prompts(project) if project else []
        titles = [prompt.title or "(untitled)" for prompt in prompts]
        self.prompt_combo["values"] = titles
        if titles:
            if self.prompt_var.get() not in titles:
                self.prompt_var.set(titles[0])
            self.preview_selected_prompt()
            self.hint.config(text=f"Project '{project}' has {len(titles)} prompt(s).")
        else:
            self.prompt_var.set("")
            self.prompt_preview_area.clear()
            self.hint.config(text=f"Project '{project}' has no prompts. Add one first in Manage Prompts.")

    def get_selected_prompt_content(self) -> str | None:
        project = self.project_var.get()
        title = self.prompt_var.get()
        if not project or not title:
            return None
        for prompt in self.service.get_prompts(project):
            if (prompt.title or "(untitled)") == title:
                return prompt.content
        return None

    def preview_selected_prompt(self) -> None:
        content = self.get_selected_prompt_content() or ""
        self.prompt_preview_area.set_text(content)

    def clear_subject(self) -> None:
        self.subject_area.clear()
        self.hint.config(text="Subject cleared (not saved).")

    def clear_final(self) -> None:
        self.final_area.clear()
        self.hint.config(text="Final input cleared.")
        self.final_area.focus_set()

    def generate(self) -> None:
        subject = self.subject_area.get_text()
        prompt_content = self.get_selected_prompt_content()
        if prompt_content is None:
            messagebox.showwarning("Missing prompt", "Please select a project and prompt first.", parent=self.app)
            return

        style_key = self.sep_var.get()
        if subject.strip():
            final = compose_final_input(prompt_content, subject, style_key)
        else:
            final = compose_prompt_with_attachments_hint(prompt_content, style_key)

        self.final_area.set_text(final)
        self.hint.config(text="Generated final input.")

    def copy_final(self) -> None:
        text = self.final_area.get_text()
        if not text.strip():
            messagebox.showwarning("Nothing to copy", "Generate the final input first.", parent=self.app)
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        messagebox.showinfo("Copied", "Final input copied to clipboard.", parent=self.app)


class HandlePipelinePage(BasePage):
    def __init__(self, parent: tk.Misc, app: "PromptManagerApp") -> None:
        super().__init__(parent, app)
        self.variable_texts: dict[str, TextAreaWithScrollbar] = {}
        self._variables_canvas_window: int | None = None

        ttk.Label(self, text="Handle Pipeline", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            self,
            text=(
                "Pipeline mode: select a project, fill every {{VARIABLE}} detected across its prompts, "
                "then choose one prompt and generate the replaced version."
            ),
            style="Muted.TLabel",
            wraplength=940,
            justify="left",
        ).pack(anchor="w", pady=(4, 14))

        nav = tk.Frame(self, bg=self.theme.palette.window_bg)
        nav.pack(fill="x", pady=(0, 12))
        AccessibleButton(nav, self.theme, text="← Home", command=lambda: app.show("HomePage"), kind="neutral").pack(side="left")
        AccessibleButton(nav, self.theme, text="Handle Subject", command=lambda: app.show("HandleSubjectPage"), kind="neutral").pack(side="left", padx=(12, 0))

        main = tk.Frame(self, bg=self.theme.palette.window_bg)
        main.pack(fill="both", expand=True)

        left = Card(main, self.theme)
        right = Card(main, self.theme)
        left.pack(side="left", fill="both", expand=True, padx=(0, 10))
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        lf = tk.Frame(left, bg=self.theme.palette.card_bg)
        lf.pack(fill="both", expand=True, padx=18, pady=18)
        lf.grid_columnconfigure(0, weight=1)
        lf.grid_columnconfigure(1, weight=1)
        lf.grid_rowconfigure(5, weight=1)

        ttk.Label(lf, text="Project / pipeline", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.project_var = tk.StringVar()
        self.project_combo = ttk.Combobox(lf, textvariable=self.project_var, state="readonly", width=30, style="Accessible.TCombobox")
        self.project_combo.grid(row=1, column=0, sticky="we", pady=(8, 12), padx=(0, 8))
        self.project_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_prompts_for_project())

        ttk.Label(lf, text="Prompt to generate", style="Section.TLabel").grid(row=0, column=1, sticky="w")
        self.prompt_var = tk.StringVar()
        self.prompt_combo = ttk.Combobox(lf, textvariable=self.prompt_var, state="readonly", width=34, style="Accessible.TCombobox")
        self.prompt_combo.grid(row=1, column=1, sticky="we", pady=(8, 12))
        self.prompt_combo.bind("<<ComboboxSelected>>", lambda _e: self.preview_selected_prompt())

        buttons = tk.Frame(lf, bg=self.theme.palette.card_bg)
        buttons.grid(row=2, column=0, columnspan=2, sticky="we", pady=(0, 12))
        AccessibleButton(buttons, self.theme, text="Generate edited prompt", command=self.generate_pipeline_prompt).pack(side="left")
        AccessibleButton(buttons, self.theme, text="Clear variables", command=self.clear_variables, kind="neutral").pack(side="left", padx=12)

        self.summary = ttk.Label(
            lf,
            text="Select a project to detect variables like {{CONTEXT CARD}} across all prompts in it.",
            style="Muted.Card.TLabel",
            wraplength=620,
            justify="left",
        )
        self.summary.grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Label(lf, text="Variable values", style="Section.TLabel").grid(row=4, column=0, columnspan=2, sticky="w")
        variables_box = tk.Frame(lf, bg=self.theme.palette.card_bg)
        variables_box.grid(row=5, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        variables_box.grid_columnconfigure(0, weight=1)
        variables_box.grid_rowconfigure(0, weight=1)

        self.variables_canvas = tk.Canvas(
            variables_box,
            bg=self.theme.palette.card_bg,
            highlightthickness=1,
            highlightbackground=self.theme.palette.border,
            bd=0,
        )
        self.variables_scroll = ttk.Scrollbar(variables_box, orient="vertical", command=self.variables_canvas.yview)
        self.variables_frame = tk.Frame(self.variables_canvas, bg=self.theme.palette.card_bg)
        self._variables_canvas_window = self.variables_canvas.create_window((0, 0), window=self.variables_frame, anchor="nw")
        self.variables_canvas.configure(yscrollcommand=self.variables_scroll.set)
        self.variables_canvas.grid(row=0, column=0, sticky="nsew")
        self.variables_scroll.grid(row=0, column=1, sticky="ns")
        self.variables_frame.bind("<Configure>", lambda _e: self._sync_variables_scrollregion())
        self.variables_canvas.bind("<Configure>", lambda event: self._resize_variables_frame(event.width))
        self.variables_canvas.bind("<MouseWheel>", self._on_variables_mousewheel)
        self.variables_frame.bind("<MouseWheel>", self._on_variables_mousewheel)

        self.hint = ttk.Label(lf, text="", style="Muted.Card.TLabel", wraplength=620, justify="left")
        self.hint.grid(row=6, column=0, columnspan=2, sticky="w", pady=(12, 0))

        rf = tk.Frame(right, bg=self.theme.palette.card_bg)
        rf.pack(fill="both", expand=True, padx=18, pady=18)
        rf.grid_columnconfigure(0, weight=1)
        rf.grid_rowconfigure(1, weight=1)
        rf.grid_rowconfigure(4, weight=2)

        ttk.Label(rf, text="Selected prompt preview", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.prompt_preview_area = TextAreaWithScrollbar(rf, self.theme, height=10, read_only=True)
        self.prompt_preview_area.grid(row=1, column=0, sticky="nsew", pady=(8, 14))

        ttk.Label(rf, text="Edited prompt output", style="Section.TLabel").grid(row=2, column=0, sticky="w")
        output_buttons = tk.Frame(rf, bg=self.theme.palette.card_bg)
        output_buttons.grid(row=3, column=0, sticky="w", pady=(8, 8))
        AccessibleButton(output_buttons, self.theme, text="Copy edited prompt", command=self.copy_output).pack(side="left")
        AccessibleButton(output_buttons, self.theme, text="Clear output", command=self.clear_output, kind="neutral").pack(side="left", padx=12)

        self.output_area = TextAreaWithScrollbar(rf, self.theme, height=14)
        self.output_area.grid(row=4, column=0, sticky="nsew")

        self.refresh()

    def on_show(self) -> None:
        if not self.service.list_projects():
            messagebox.showinfo(
                "No projects yet",
                "Please add a project and a prompt first.",
                parent=self.app,
            )
            self.app.show("ManageProjectsPage")
            return
        self.project_combo.focus_set()

    def refresh(self) -> None:
        projects = self.service.list_projects()
        self.project_combo["values"] = projects
        if projects and self.project_var.get() not in projects:
            self.project_var.set(projects[0])
        self.refresh_prompts_for_project()

    def _sync_variables_scrollregion(self) -> None:
        self.variables_canvas.configure(scrollregion=self.variables_canvas.bbox("all"))

    def _resize_variables_frame(self, width: int) -> None:
        if self._variables_canvas_window is not None:
            self.variables_canvas.itemconfigure(self._variables_canvas_window, width=width)

    def _on_variables_mousewheel(self, event: tk.Event) -> str:
        delta = -1 if event.delta > 0 else 1
        self.variables_canvas.yview_scroll(delta, "units")
        return "break"

    def _remember_values(self) -> dict[str, str]:
        return {name: area.get_text() for name, area in self.variable_texts.items()}

    def refresh_prompts_for_project(self) -> None:
        project = self.project_var.get()
        prompts = self.service.get_prompts(project) if project else []
        titles = [prompt.title or "(untitled)" for prompt in prompts]
        self.prompt_combo["values"] = titles
        if titles:
            if self.prompt_var.get() not in titles:
                self.prompt_var.set(titles[0])
            self.preview_selected_prompt()
        else:
            self.prompt_var.set("")
            self.prompt_preview_area.clear()
        self.refresh_variable_fields(prompts)

    def refresh_variable_fields(self, prompts: list[PromptRecord]) -> None:
        old_values = self._remember_values()
        for child in self.variables_frame.winfo_children():
            child.destroy()
        self.variable_texts.clear()

        project = self.project_var.get() or "selected project"
        variables = extract_project_variables(prompts)
        if not prompts:
            self.summary.config(text=f"No prompts found in '{project}'.")
            self._sync_variables_scrollregion()
            return
        if not variables:
            self.summary.config(
                text=(
                    f"No {{VARIABLE}} placeholders found in '{project}'. Use double braces, "
                    "for example {{CONTEXT CARD}}."
                )
            )
            self._sync_variables_scrollregion()
            return

        self.summary.config(
            text=(
                f"Detected {len(variables)} variable(s) across {len(prompts)} prompt(s) in '{project}'. "
                "Fill them once; only variables used by the selected prompt are required at generation time."
            )
        )

        for index, variable in enumerate(variables, start=1):
            label = tk.Label(
                self.variables_frame,
                text=f"{index}. {variable}",
                bg=self.theme.palette.card_bg,
                fg=self.theme.palette.text,
                font=self.theme.fonts.section,
                anchor="w",
                justify="left",
            )
            label.grid(row=(index - 1) * 2, column=0, sticky="we", padx=8, pady=(12, 2))
            area = TextAreaWithScrollbar(self.variables_frame, self.theme, height=5)
            area.grid(row=(index - 1) * 2 + 1, column=0, sticky="we", padx=8, pady=(0, 8))
            if variable in old_values:
                area.set_text(old_values[variable])
            self.variable_texts[variable] = area

        self.variables_frame.grid_columnconfigure(0, weight=1)
        self._sync_variables_scrollregion()

    def get_selected_prompt_content(self) -> str | None:
        project = self.project_var.get()
        title = self.prompt_var.get()
        if not project or not title:
            return None
        for prompt in self.service.get_prompts(project):
            if (prompt.title or "(untitled)") == title:
                return prompt.content
        return None

    def preview_selected_prompt(self) -> None:
        content = self.get_selected_prompt_content() or ""
        self.prompt_preview_area.set_text(content)
        variables = extract_prompt_variables(content)
        if variables:
            self.hint.config(text=f"Selected prompt uses {len(variables)} variable(s): {', '.join(variables)}")
        else:
            self.hint.config(text="Selected prompt has no variables.")

    def clear_variables(self) -> None:
        if not self.variable_texts:
            self.hint.config(text="No variable fields to clear.")
            return
        for area in self.variable_texts.values():
            area.clear()
        self.hint.config(text="Variable values cleared (not saved).")

    def clear_output(self) -> None:
        self.output_area.clear()
        self.hint.config(text="Output cleared.")
        self.output_area.focus_set()

    def generate_pipeline_prompt(self) -> None:
        prompt_content = self.get_selected_prompt_content()
        if prompt_content is None:
            messagebox.showwarning("Missing prompt", "Please select a project and prompt first.", parent=self.app)
            return
        if not self.variable_texts:
            messagebox.showwarning(
                "No variables found",
                "This project has no {{VARIABLE}} placeholders to replace.",
                parent=self.app,
            )
            return

        values = {name: area.get_text() for name, area in self.variable_texts.items()}
        selected_variables = extract_prompt_variables(prompt_content)
        missing = [name for name in selected_variables if not values.get(name, "").strip()]
        if missing:
            proceed = messagebox.askyesno(
                "Empty variables",
                (
                    "These variable fields are empty for the selected prompt:\n\n"
                    + "\n".join(f"- {name}" for name in missing)
                    + "\n\nGenerate anyway?"
                ),
                parent=self.app,
            )
            if not proceed:
                return

        output = render_prompt_template(prompt_content, values)
        self.output_area.set_text(output)
        self.hint.config(
            text=(
                f"Generated edited prompt from '{self.prompt_var.get()}'. "
                f"Replaced {len(selected_variables)} variable occurrence group(s)."
            )
        )

    def copy_output(self) -> None:
        text = self.output_area.get_text()
        if not text.strip():
            messagebox.showwarning("Nothing to copy", "Generate the edited prompt first.", parent=self.app)
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        messagebox.showinfo("Copied", "Edited prompt copied to clipboard.", parent=self.app)


# ---------------------------------------------------------------------------
# Application shell
# ---------------------------------------------------------------------------


class PromptManagerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1280x860")
        self.minsize(1080, 740)

        self.repository = StoreRepository()
        self.service = PromptManagerService(self.repository)
        self.theme = ThemeManager(self)

        self.bind_all("<Command-q>", lambda _event: self.destroy())
        self.bind_all("<Command-1>", lambda _event: self.show("HomePage"))
        self.bind_all("<Command-2>", lambda _event: self.show("ManageProjectsPage"))
        self.bind_all("<Command-3>", lambda _event: self.show("ManagePromptsPage"))
        self.bind_all("<Command-4>", lambda _event: self.show("HandleSubjectPage"))
        self.bind_all("<Command-5>", lambda _event: self.show("HandlePipelinePage"))

        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)

        self._build_top_bar(outer)

        self.status_var = tk.StringVar(value="Ready.")
        self.status_label = tk.Label(
            outer,
            textvariable=self.status_var,
            bg=self.theme.palette.status_bg,
            fg=self.theme.palette.text,
            font=self.theme.fonts.small,
            anchor="w",
            padx=12,
            pady=8,
            relief="solid",
            bd=1,
            highlightthickness=1,
            highlightbackground=self.theme.palette.border,
        )
        self.status_label.pack(fill="x", pady=(0, 12))

        self.container = ttk.Frame(outer)
        self.container.pack(fill="both", expand=True)
        self.container.grid_columnconfigure(0, weight=1)
        self.container.grid_rowconfigure(0, weight=1)

        self.pages: dict[str, BasePage] = {}
        for page_cls in (HomePage, ManageProjectsPage, ManagePromptsPage, HandleSubjectPage, HandlePipelinePage):
            page = page_cls(self.container, self)
            self.pages[page_cls.__name__] = page
            page.grid(row=0, column=0, sticky="nsew")

        if self.service.load_warning:
            self.after(50, lambda: messagebox.showerror("Storage Error", self.service.load_warning, parent=self))

        self.show("HomePage")

    def _build_top_bar(self, parent: tk.Misc) -> None:
        bar = Card(parent, self.theme)
        bar.pack(fill="x", pady=(0, 12))
        inner = tk.Frame(bar, bg=self.theme.palette.card_bg)
        inner.pack(fill="x", padx=18, pady=14)

        ttk.Label(inner, text=APP_NAME, style="Section.TLabel").pack(side="left")

        nav = tk.Frame(inner, bg=self.theme.palette.card_bg)
        nav.pack(side="right")
        AccessibleButton(nav, self.theme, text="Home", command=lambda: self.show("HomePage"), kind="neutral").pack(side="left")
        AccessibleButton(nav, self.theme, text="Projects", command=lambda: self.show("ManageProjectsPage"), kind="neutral").pack(side="left", padx=(10, 0))
        AccessibleButton(nav, self.theme, text="Prompts", command=lambda: self.show("ManagePromptsPage"), kind="neutral").pack(side="left", padx=(10, 0))
        AccessibleButton(nav, self.theme, text="Subject", command=lambda: self.show("HandleSubjectPage"), kind="neutral").pack(side="left", padx=(10, 0))
        AccessibleButton(nav, self.theme, text="Pipeline", command=lambda: self.show("HandlePipelinePage"), kind="neutral").pack(side="left", padx=(10, 0))

    def set_status(self, message: str) -> None:
        self.status_var.set(message)

    def refresh_all_pages(self) -> None:
        warning = self.service.reload()
        for page in self.pages.values():
            page.refresh()
        if warning:
            messagebox.showerror("Storage Error", warning, parent=self)

    def open_storage_folder(self) -> None:
        try:
            subprocess.Popen(["open", str(APP_SUPPORT_DIR)])
        except Exception as exc:
            messagebox.showerror("Open failed", f"Could not open:\n{APP_SUPPORT_DIR}\n\nReason: {exc}", parent=self)

    def import_store_dialog(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self,
            title="Choose a store.json file",
            initialdir=str(Path.home()),
            filetypes=[("JSON files", "*.json"), ("All files", "*")],
        )
        if not chosen:
            return
        try:
            store = self.service.import_store_from_path(Path(chosen).expanduser())
        except Exception as exc:
            messagebox.showerror("Import failed", str(exc), parent=self)
            return
        self.refresh_all_pages()
        messagebox.showinfo(
            "Store imported",
            (
                f"Imported {store.project_count()} project(s) and {store.prompt_count()} prompt(s).\n\n"
                f"Source:\n{Path(chosen).expanduser()}\n\n"
                f"Active store:\n{self.repository.active_store_path}"
            ),
            parent=self,
        )

    def show(self, page_name: str) -> None:
        page = self.pages[page_name]
        page.tkraise()
        page.on_show()
        self.set_status(f"Current view: {page_name.replace('Page', '')}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    app = PromptManagerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
