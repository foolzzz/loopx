"""The README smoke must accept current hosts and still detect missing CLI entry points."""

from pathlib import Path
import runpy

import pytest


SMOKE = (
    Path(__file__).resolve().parents[1]
    / "examples/public_entry/readme-demo-surface-smoke.py"
)


def test_readme_smoke_accepts_current_public_docs():
    smoke = runpy.run_path(str(SMOKE))
    assert smoke["main"]() == 0


@pytest.mark.parametrize("readme", ["README.md", "README.zh-CN.md"])
def test_readme_smoke_requires_codex_cli_in_each_language(monkeypatch, readme):
    smoke = runpy.run_path(str(SMOKE))
    read = smoke["read"]

    def missing_cli(path):
        text = read(path)
        if path == readme:
            assert "Codex CLI" in text
            return text.replace("Codex CLI", "Removed host")
        return text

    monkeypatch.setitem(smoke["main"].__globals__, "read", missing_cli)
    with pytest.raises(AssertionError, match="^Codex CLI$"):
        smoke["main"]()
