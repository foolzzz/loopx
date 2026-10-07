#!/usr/bin/env python3
"""Validate the public update-note archive and source boundary."""

from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
DOCS_INDEX = ROOT / "docs" / "README.md"
NOTES_DIR = ROOT / "docs" / "update-notes"
NOTES_INDEX = NOTES_DIR / "README.md"
NOTE_FILE_RE = re.compile(r"\d{4}-\d{2}-\d{2}-to-\d{4}-\d{2}-\d{2}\.md$")

FORBIDDEN_PUBLIC_STRINGS = [
    "/Users/",
    "/private/tmp/",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "raw_thread",
    "session_history",
    "verifier_output_tail",
    "ACTIVE_GOAL_STATE.md:",
]


def read(path: Path) -> str:
    if not path.exists():
        raise AssertionError(f"missing expected file: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def assert_contains(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} is missing {needle!r}")


def assert_not_contains(text: str, needle: str, label: str) -> None:
    if needle in text:
        raise AssertionError(f"{label} contains forbidden string {needle!r}")


def validate_public_boundary(path: Path) -> None:
    text = read(path)
    label = str(path.relative_to(ROOT))
    for forbidden in FORBIDDEN_PUBLIC_STRINGS:
        assert_not_contains(text, forbidden, label)


def note_files() -> list[Path]:
    files = sorted(path for path in NOTES_DIR.glob("*.md") if NOTE_FILE_RE.match(path.name))
    if len(files) < 2:
        raise AssertionError("expected at least two archived update notes")
    return files


def validate_indexes() -> None:
    root_readme = read(README)
    docs_index = read(DOCS_INDEX)
    notes_index = read(NOTES_INDEX)
    files = note_files()

    assert_contains(root_readme, "docs/update-notes/README.md", "root README")
    assert_contains(docs_index, "update-notes/README.md", "docs README")
    for note in files:
        assert_contains(notes_index, note.name, "notes index")
    assert_contains(notes_index, files[-1].name, "notes index latest")


def validate_notes() -> None:
    for note in note_files():
        text = read(note)
        label = str(note.relative_to(ROOT))
        assert_contains(text, "# Biweekly Update Note:", label)
        assert_contains(text, "## Source Boundary", label)
        assert_contains(text, "## Highlights", label)
        assert_contains(text, "## What Shipped", label)
        assert_contains(text, "## Validation And Public Boundary", label)


def main() -> None:
    for path in [
        README,
        DOCS_INDEX,
        NOTES_INDEX,
        *note_files(),
    ]:
        validate_public_boundary(path)
    validate_indexes()
    validate_notes()
    print("update notes archive smoke: ok")


if __name__ == "__main__":
    main()
