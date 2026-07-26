#!/bin/zsh

if [[ -n "${1:-}" && -d "$1" ]]; then
  TARGET_DIR="${1:A}"
else
  TARGET_DIR="$(cd "$(dirname "$0")" && pwd)"
fi

SCRIPT_PATH="${0:A}"

python3 - "$TARGET_DIR" "$SCRIPT_PATH" <<'PYTHON'
from __future__ import annotations

import os
import re
import sys
import unicodedata
from pathlib import Path


target_dir = Path(sys.argv[1])
script_path = Path(sys.argv[2]).resolve()


def split_base_and_suffix(name: str) -> tuple[str, str]:
    # Keep subtitle descriptors/languages attached to the extension so related
    # media retain one shared base: video.mp4, video.ar.srt, video.bilingual.ass.
    subtitle_match = re.search(
        r"(?P<suffix>(?:\.[A-Za-z0-9_-]{2,20})?\.(?:srt|ass|vtt))$",
        name,
        flags=re.IGNORECASE,
    )
    if subtitle_match:
        return name[: subtitle_match.start()], subtitle_match.group("suffix")

    suffix = Path(name).suffix
    if suffix:
        return name[: -len(suffix)], suffix
    return name, ""


def ascii_safe_base(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.replace("&", " and ")
    ascii_text = re.sub(r"[^A-Za-z0-9]+", " ", ascii_text)
    ascii_text = re.sub(r"\s+", " ", ascii_text).strip()
    return ascii_text or "file"


files = sorted(
    (
        path
        for path in target_dir.iterdir()
        if path.is_file() and path.resolve() != script_path
    ),
    key=lambda path: os.fsencode(path.name),
)

plan: list[tuple[Path, Path]] = []
destination_sources: dict[str, list[str]] = {}
existing_names = {path.name for path in files}

for source in files:
    base, suffix = split_base_and_suffix(source.name)
    destination_name = f"{ascii_safe_base(base)}{suffix}"
    destination = source.with_name(destination_name)
    if destination_name != source.name:
        plan.append((source, destination))
        destination_sources.setdefault(destination_name, []).append(source.name)

collisions = {
    destination: sources
    for destination, sources in destination_sources.items()
    if len(sources) > 1
}
for source, destination in plan:
    if destination.name in existing_names and destination.name != source.name:
        collisions.setdefault(destination.name, []).append(source.name)

print(f"Folder: {target_dir}")
print()

if not plan:
    print("All filenames are already ASCII-safe. Nothing to rename.")
    raise SystemExit(0)

if collisions:
    print("ERROR: Nothing was renamed because these destination names would collide:")
    for destination, sources in sorted(collisions.items()):
        print(f"  {destination}")
        for source in sorted(set(sources)):
            print(f"    <- {source}")
    raise SystemExit(1)

print(f"Planned renames: {len(plan)}")
print()
for source, destination in plan:
    print(f"FROM: {source.name}")
    print(f"  TO: {destination.name}")
    print()

confirmation = os.environ.get("ASCII_RENAME_CONFIRM")
if confirmation is None:
    try:
        with open("/dev/tty", "r+", encoding="utf-8") as terminal:
            terminal.write("Type YES to rename these files: ")
            terminal.flush()
            confirmation = terminal.readline()
    except OSError:
        confirmation = ""
confirmation = confirmation.strip()
if confirmation != "YES":
    print("Cancelled. Nothing was renamed.")
    raise SystemExit(0)

# Use temporary names first so case-only and normalization-only changes are
# safe even on case-insensitive filesystems.
temporary_moves: list[tuple[Path, Path, Path]] = []
try:
    for index, (source, destination) in enumerate(plan, start=1):
        temporary = source.with_name(f".ascii-rename-{os.getpid()}-{index}.tmp")
        source.rename(temporary)
        temporary_moves.append((source, temporary, destination))

    for _source, temporary, destination in temporary_moves:
        temporary.rename(destination)
except Exception:
    # Best-effort rollback for temporary entries that have not reached their
    # final destination.
    for source, temporary, destination in reversed(temporary_moves):
        try:
            if temporary.exists() and not source.exists():
                temporary.rename(source)
            elif destination.exists() and not source.exists():
                destination.rename(source)
        except OSError:
            pass
    raise

print()
print(f"Done. Renamed {len(plan)} file(s).")
PYTHON

STATUS=$?
echo
if [[ -t 0 ]]; then
  read -k 1 "?Press any key to close..."
fi
exit "$STATUS"
