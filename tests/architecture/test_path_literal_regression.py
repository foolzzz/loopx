"""Keep path knowledge in loopx.paths; existing non-owner literals only shrink.

The budget is a per-file multiset of literal fingerprints, not a global count.
Labels, validation strings, generated-source templates and standalone providers
remain visible in the inventory without allowing new literal values elsewhere.
Python comments/docstrings and JS comments are prose, not construction sites.
Shell heredocs are scanned too: they can contain executable generated source.
"""

from __future__ import annotations

import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BUDGET_PATH = Path(__file__).with_name("fixtures") / "path_literal_budget.json"
SHELL_SUFFIXES = {".sh", ".bash", ".zsh", ".ps1"}
SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"} | SHELL_SUFFIXES
PROSE_ROOTS = {"docs", "benchmark", "deprecate", "examples", "tests", "regression"}
EXCLUDED_PARTS = {"tests", "test", "testing", "__tests__", "smoke", "smokes", "node_modules"}
PATH_LITERAL = re.compile(r"(?<![\w.-])\.(?:loopx|codex)(?![\w.-])")
QUOTED = r'''"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*'|`(?:\\[\s\S]|[^`\\])*`'''
JS_TOKENS = re.compile(r"(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)|(?P<literal>" + QUOTED + ")")
SHELL_TOKENS = re.compile(
    r"(?P<comment>\#[^\n]*)|(?P<literal>" + QUOTED + r")|(?P<bare>[^\s\"'`#]+)"
)


def _is_production_source(path: str) -> bool:
    relative = Path(path)
    name = relative.name
    return (
        relative.suffix in SOURCE_SUFFIXES
        and relative.parts[0] not in PROSE_ROOTS
        and not EXCLUDED_PARTS.intersection(relative.parts)
        and not name.startswith(("test_", "test-"))
        and not re.search(r"(?:^|[._-])(?:test|spec|smoke)(?:[._-]|$)", name)
        and path != "loopx/paths.py"
    )


def _tracked_sources(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root, check=True, capture_output=True,
    )
    # Deleted tracked files do not retain an allowance in the current scan.
    return sorted({
        name
        for name in result.stdout.decode().split("\0")
        if name and _is_production_source(name) and (root / name).is_file()
    })


def _python_literals(path: str, source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source, filename=path)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr):
                value = node.body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docstrings.add(id(value))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _source_literals(path: str, source: str) -> list[tuple[int, str]]:
    if Path(path).suffix == ".py":
        return _python_literals(path, source)
    lexer = SHELL_TOKENS if Path(path).suffix in SHELL_SUFFIXES else JS_TOKENS
    literals = []
    line = 1
    offset = 0
    for token in lexer.finditer(source):
        line += source.count("\n", offset, token.start())
        offset = token.start()
        if token.lastgroup != "comment":
            literals.append((line, token.group()))
    return literals


def _inventory(sources: dict[str, str]) -> dict[str, Counter[str]]:
    inventory: dict[str, Counter[str]] = {}
    for path, source in sorted(sources.items()):
        if not _is_production_source(path):
            continue
        counts: Counter[str] = Counter()
        for _, literal in _source_literals(path, source):
            occurrences = len(PATH_LITERAL.findall(literal))
            if occurrences:
                # The fixture stores no full literals or local filesystem paths.
                fingerprint = hashlib.sha256(literal.encode()).hexdigest()
                counts[fingerprint] += occurrences
        if counts:
            inventory[path] = counts
    return inventory


def _repository_inventory(root: Path) -> dict[str, Counter[str]]:
    return _inventory({path: (root / path).read_text() for path in _tracked_sources(root)})


def _regressions(
    current: dict[str, Counter[str]], budget: dict[str, Counter[str]]
) -> dict[str, Counter[str]]:
    return {
        path: excess
        for path, counts in current.items()
        if (excess := counts - budget.get(path, Counter()))
    }


@pytest.mark.parametrize(
    ("path", "source", "count"),
    [
        ("loopx/probe.py", 'root = Path.home() / ".loopx"', 1),
        ("loopx/probe.py", 'root = (Path.home()\n / ".codex"\n / "skills")', 1),
        ("loopx/probe.py", 'root = os.path.join(os.path.expanduser("~"), ".loopx", "goals")', 1),
        ("loopx/probe.py", 'root = "~/.loopx/goals/example"', 1),
        ("loopx/probe.py", 'root = ".loopx/goals"', 1),
        ("loopx/probe.py", 'root = "/state/.loopx/goals"', 1),
        ("loopx/probe.py", 'root = f"{home}/.codex/skills/{name}"', 1),
        ("loopx/probe.py", 'template = "Path.home() / \' .codex\'.strip()"', 1),
        ("apps/probe.ts", 'const root = join(homedir(), ".loopx", "goals")', 1),
        ("packages/probe/index.ts", "const root = `${home}/.codex/config.toml`", 1),
        ("skills/probe/scripts/check.py", 'root = Path.home() / ".loopx"', 1),
        ("scripts/probe.sh", 'root="${LOOPX_RUNTIME_ROOT:-$HOME/.loopx}"', 1),
        ("scripts/probe.sh", 'root=$HOME/.codex/sessions', 1),
        ("scripts/probe.ps1", 'Join-Path $HOME ".codex"', 1),
        ("scripts/probe.sh", 'python - <<\'PY\'\nroot = Path.home() / ".codex"\nPY\n', 1),
        ("loopx/probe.py", 'a = ".loopx"; b = ".loopx"', 2),
        ("loopx/probe.py", 'a = "~/.loopx and ~/.codex"', 2),
    ],
)
def test_detector_finds_path_literal_forms(path: str, source: str, count: int) -> None:
    counts = _inventory({path: source})
    assert sum(counts[path].values()) == count


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("loopx/probe.py", '# Path.home() / ".loopx"\nroot = default_runtime_root()'),
        ("loopx/probe.py", '\'\'\'Uses ~/.codex/config.toml.\'\'\'\nroot = codex_home()'),
        ("loopx/probe.py", 'name = ".loopx-python"; other = "loopx.paths"'),
        ("apps/probe.ts", '// "~/.loopx"\n/* ".codex/skills" */\nconst x = "loopx"'),
        ("scripts/probe.sh", '# root=$HOME/.loopx\nroot="default" # .codex/skills'),
    ],
)
def test_detector_ignores_prose_and_unrelated_names(path: str, source: str) -> None:
    assert _inventory({path: source}) == {}


@pytest.mark.parametrize(
    "path",
    [
        "tests/probe.py", "loopx/control_plane/testing/probe.py",
        "packages/provider/__tests__/probe.ts", "packages/provider/src/probe.test.ts",
        "apps/src/probe.spec.tsx", "scripts/path-smoke.py", "examples/probe.py",
        "regression/probe.py", "scripts/smoke/probe.py",
        "benchmark/frozen/probe.py", "deprecate/benchmark-legacy/probe.py",
        "docs/probe.py", "README.md", "loopx/paths.py",
    ],
)
def test_detector_excludes_nonproduction_sources(path: str) -> None:
    assert _inventory({path: 'root = "~/.loopx"'}) == {}


def test_budget_cannot_trade_deleted_literals_for_new_values_or_files() -> None:
    old = {"loopx/probe.py": 'a = "~/.loopx/old"; b = ".codex/skills"'}
    budget = _inventory(old)
    assert not _regressions(_inventory({"loopx/probe.py": 'b = ".codex/skills"'}), budget)
    for changed in (
        {"loopx/probe.py": 'a = "~/.loopx/new"; b = ".codex/skills"'},
        {"loopx/probe.py": 'a = ".codex/skills"; b = ".codex/skills"'},
        {"loopx/other.py": old["loopx/probe.py"]},
    ):
        assert _regressions(_inventory(changed), budget)


def test_source_scan_includes_tracked_and_new_production_files(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name in ("loopx/tracked.py", "loopx/untracked.py", "tests/tracked.py", "packages/p/src/index.ts"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('root = "~/.loopx"')
    subprocess.run(
        ["git", "add", "loopx/tracked.py", "tests/tracked.py", "packages/p/src/index.ts"],
        cwd=tmp_path, check=True,
    )
    assert _tracked_sources(tmp_path) == [
        "loopx/tracked.py", "loopx/untracked.py", "packages/p/src/index.ts"
    ]
    assert sum(sum(rows.values()) for rows in _repository_inventory(tmp_path).values()) == 3


def test_production_path_literals_do_not_regress() -> None:
    payload = json.loads(BUDGET_PATH.read_text())
    assert payload["schema_version"] == 1
    budget = {path: Counter(rows) for path, rows in payload["files"].items()}
    current = _repository_inventory(REPOSITORY_ROOT)
    assert _tracked_sources(REPOSITORY_ROOT), "production-source scan must not be empty"
    excess = _regressions(current, budget)
    details = []
    for path, fingerprints in excess.items():
        source = (REPOSITORY_ROOT / path).read_text()
        lines = [
            str(line) for line, literal in _source_literals(path, source)
            if hashlib.sha256(literal.encode()).hexdigest() in fingerprints
        ]
        details.append(f"{path}:{','.join(lines)} (+{sum(fingerprints.values())})")
    assert not excess, (
        "New raw path literals bypass loopx.paths; use its helpers rather than "
        "raising the residual budget: " + "; ".join(details)
    )
