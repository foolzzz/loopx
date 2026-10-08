"""Keep path knowledge in loopx.paths; existing non-owner literals only shrink.

The budget is a per-file multiset of literal fingerprints, not a global count.
Labels, validation strings, generated-source templates and standalone providers
remain visible in the inventory without allowing new literal values elsewhere.
Python comments/docstrings and JS comments are prose, not construction sites.
Shell heredocs are scanned too: they can contain executable generated source.
Python/JS string-only + expressions are folded and shell word parts are joined.
PowerShell literals are decoded, but its context-sensitive expressions are not
folded without a language parser. No variables or calls are evaluated.
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
PROSE_ROOTS = {"docs", "examples", "tests", "regression"}
EXCLUDED_PARTS = {"tests", "test", "testing", "__tests__", "smoke", "smokes", "node_modules"}
PATH_LITERAL = re.compile(r"(?<![\w.-])\.(?:loopx|codex)(?![\w.-])")
SHELL_TOKENS = re.compile(
    # Preserve comment boundaries: a hash inside a shell word is content.
    r"(?P<comment>(?<![^\s;&|()<>])\#[^\n]*)|(?P<operator>[;|&()<>])|(?P<literal>"
    r"\$'(?:\\[\s\S]|[^'\\])*'|'[^']*'|"
    r'"(?:\\[\s\S]|[^"\\])*"|`[^`]*`)'
    r"|(?P<bare>(?:(?!\$')(?:\\[\s\S]|[^\s\"'`;|&()<>\\]))+)"
)
POWERSHELL_QUOTED = r'"(?:`[\s\S]|""|[^"`])*"' + r"|'(?:''|[^'])*'"
POWERSHELL_BARE = r"(?:`[\s\S]|[^\s\"'`;&,|(){}\[\]])"
POWERSHELL_WORD = (
    POWERSHELL_BARE + r"+(?:(?:" + POWERSHELL_QUOTED + ")" + POWERSHELL_BARE + r"*)*"
)
POWERSHELL_TOKENS = re.compile(
    # PowerShell also has block comments and uses backticks for escaping.
    # Consume embedded quotes with their word; a completed block is a boundary.
    r"(?P<comment>(?<![^\s;&,|(){}\[\]'\">=])(?:<\#[\s\S]*?\#>|\#[^\n]*))"
    # Assignment prefixes are syntax, not the start of a quoted command word.
    r"|(?P<operator>[;&,|(){}\[\]])"
    r"|(?P<assignment>(?:\$(?:[\w:]+|\{[^}]*\}))?=)"
    r"|(?P<literal>" + POWERSHELL_QUOTED
    + r")|(?P<bare>" + POWERSHELL_WORD + ")"
)

ANSI_ATOM = r"x[0-9a-fA-F]{1,2}|u[0-9a-fA-F]{1,4}|U[0-9a-fA-F]{1,8}|[0-7]{1,3}|[\s\S]"
ANSI_ESCAPE = re.compile(r"\\(?:c[\s\S]|" + ANSI_ATOM + ")")
ZSH_ESCAPE = re.compile(
    r"\\(?:(?P<modifier>[CM])-?|(?P<atom>" + ANSI_ATOM + r"))|(?P<plain>[\s\S])"
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

    def constant(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = constant(node.left), constant(node.right)
            if left is not None and right is not None:
                return left + right
        return None

    literals = []

    def visit(node: ast.AST) -> None:
        value = constant(node)
        if value is not None:
            if id(node) not in docstrings:
                literals.append((node.lineno, value))
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return literals


def _javascript_literals(sources: dict[str, str]) -> dict[str, list[tuple[int, str]]]:
    if not sources:
        return {}
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("path-literals.mjs"))],
        input=json.dumps(sources), text=True, capture_output=True, check=True,
    )
    return json.loads(result.stdout)


def _shell_value(value: str) -> str:
    # Reassemble byte escapes before decoding; retain invalid UTF-8 as surrogates.
    return value.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="surrogateescape")


def _ansi_literal(raw: str, *, bash_controls: bool) -> str:
    def decode_escape(escape: str) -> str:
        if bash_controls and escape == "c":
            return "\\"
        if escape[0] in "xuU" and (len(escape) > 1 or not bash_controls):
            # Zsh's numeric escapes with no digits produce NUL.
            value = int(escape[1:] or "0", 16)
            if escape[0] == "x":
                return bytes([value]).decode("utf-8", errors="surrogateescape")
            if value <= 0x10FFFF:
                return chr(value).encode("utf-8", errors="surrogatepass").decode("utf-8", errors="surrogateescape")
            if not bash_controls:
                # Zsh emits extended UTF-8 for the full 32-bit \U range.
                width = 4 if value <= 0x1FFFFF else 5 if value <= 0x3FFFFFF else 6
                leading = ((0xFF << (8 - width)) & 0xFF) | (value >> (6 * (width - 1)))
                encoded = bytes([leading, *(
                    0x80 | ((value >> shift) & 0x3F)
                    for shift in range(6 * (width - 2), -1, -6)
                )])
                return encoded.decode("utf-8", errors="surrogateescape")
            return "\\" + escape
        if bash_controls and escape.startswith("c") and len(escape) == 2:
            # Bash transforms the first byte, retaining UTF-8 continuation bytes.
            encoded = escape[1].encode("utf-8")
            return (bytes([encoded[0] & 31]) + encoded[1:]).decode("utf-8", errors="surrogateescape")
        if escape[0] in "01234567":
            return bytes([int(escape, 8) % 256]).decode("utf-8", errors="surrogateescape")
        return {
            "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f",
            "n": "\n", "r": "\r", "t": "\t", "v": "\v",
            "\\": "\\", "'": "'", '"': '"', "?": "?",
            "\n": "\\\n" if bash_controls else "\n",
        }.get(escape, "\\" + escape if bash_controls else escape)

    control = False
    meta = 0

    def decode(match: re.Match[str]) -> str:
        nonlocal control, meta
        if bash_controls:
            return decode_escape(match.group()[1:])
        # Zsh getkeystring keeps modifier flags across Unicode escapes.
        if match["modifier"]:
            if match["modifier"] == "C":
                control = True
            else:
                meta = 1 + int(control)
            return ""
        value = (
            match["plain"] if match["plain"] is not None
            else decode_escape(match["atom"])
        )
        if (match["atom"] or "").startswith(("u", "U")) or not (control or meta):
            return value
        encoded = value.encode("utf-8", errors="surrogateescape")
        codepoint = encoded[0]
        if meta == 2:
            codepoint |= 0x80
        if control:
            codepoint = 0x7F if codepoint == 0x3F else codepoint & 0x9F
        if meta == 1:
            codepoint |= 0x80
        control, meta = False, 0
        return (bytes([codepoint]) + encoded[1:]).decode("utf-8", errors="surrogateescape")

    value = _shell_value((ANSI_ESCAPE if bash_controls else ZSH_ESCAPE).sub(decode, raw))
    # Bash strings end at NUL within this part; Zsh strings retain embedded NULs.
    return value.split("\0", 1)[0] if bash_controls else value


def _shell_literal(raw: str, powershell: bool, *, bash_controls: bool = True) -> str:
    if not powershell and raw.startswith("$'"):
        return _ansi_literal(raw[2:-1], bash_controls=bash_controls)
    if raw.startswith("'"):
        return raw[1:-1].replace("''", "'") if powershell else raw[1:-1]
    if raw.startswith('"'):
        if powershell:
            def decode(match: re.Match[str]) -> str:
                if match.group() == '""':
                    return '"'
                escape = match.group()[1:]
                if escape.startswith("u{"):
                    value = int(escape[2:-1], 16)
                    return chr(value) if value <= 0x10FFFF else match.group()
                return {
                    "0": "\0", "a": "\a", "b": "\b", "e": "\x1b", "f": "\f",
                    "n": "\n", "r": "\r", "t": "\t", "v": "\v",
                }.get(escape, escape)

            return re.sub(r'""|`(?:u\{[0-9a-fA-F]+\}|[\s\S])', decode, raw[1:-1])
        # Ordinary shell double quotes do not decode ANSI hex/unicode escapes.
        return re.sub(r'\\([\$`"\\\n])', lambda m: "" if m[1] == "\n" else m[1], raw[1:-1])
    if powershell:
        # Opaque command words retain quote boundaries; decode their literal parts.
        return re.sub(
            POWERSHELL_QUOTED,
            lambda m: m[0][0] + _shell_literal(m[0], True) + m[0][-1],
            raw,
        )
    if not raw.startswith("`"):
        return re.sub(r"\\([\s\S])", lambda m: "" if m[1] == "\n" else m[1], raw)
    return raw


def _constant_shell_part(raw: str) -> bool:
    if raw.startswith(("'", "$'")):
        return True
    # Keep variable/command fragments separate; their spelling is not their value.
    unescaped = re.sub(r"\\[\s\S]", "", raw)
    return "$" not in unescaped and "`" not in unescaped


def _shell_literals(
    source: str, powershell: bool, *, bash_controls: bool = True,
) -> list[tuple[int, str]]:
    lexer = POWERSHELL_TOKENS if powershell else SHELL_TOKENS
    literals: list[tuple[int, str]] = []
    end = 0
    previous_constant = False
    for token in lexer.finditer(source):
        if token.lastgroup in {"comment", "operator", "assignment"}:
            end = token.end()
            previous_constant = False
            continue
        value = _shell_literal(token.group(), powershell, bash_controls=bash_controls)
        constant = not powershell and _constant_shell_part(token.group())
        gap = source[end:token.start()]
        # Shell joins word parts. PowerShell + requires expression/argument mode,
        # precedence and operand ownership; leave that to a future language parser.
        joins = not powershell and not gap
        if literals and joins and constant and previous_constant:
            line, previous = literals[-1]
            literals[-1] = (line, _shell_value(previous + value))
        else:
            literals.append((source.count("\n", 0, token.start()) + 1, value))
        end = token.end()
        previous_constant = constant
    return literals


def _source_literals(path: str, source: str) -> list[tuple[int, str]]:
    suffix = Path(path).suffix
    if suffix == ".py":
        return _python_literals(path, source)
    if suffix in SHELL_SUFFIXES:
        zsh = suffix == ".zsh" or bool(re.match(r"#![^\n]*\bzsh(?:\s|$)", source))
        return _shell_literals(source, suffix == ".ps1", bash_controls=not zsh)
    return _javascript_literals({path: source})[path]


def _fingerprint(literal: str) -> str:
    # JS permits lone UTF-16 surrogates; normal values retain their UTF-8 hashes.
    return hashlib.sha256(literal.encode("utf-8", errors="surrogatepass")).hexdigest()


def _inventory(sources: dict[str, str]) -> dict[str, Counter[str]]:
    inventory: dict[str, Counter[str]] = {}
    javascript = _javascript_literals({
        path: source for path, source in sources.items()
        if _is_production_source(path) and Path(path).suffix not in SHELL_SUFFIXES | {".py"}
    })
    for path, source in sorted(sources.items()):
        if not _is_production_source(path):
            continue
        counts: Counter[str] = Counter()
        literals = javascript[path] if path in javascript else _source_literals(path, source)
        for _, literal in literals:
            occurrences = len(PATH_LITERAL.findall(literal))
            if occurrences:
                # The fixture stores no full literals or local filesystem paths.
                fingerprint = _fingerprint(literal)
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
        ("loopx/probe.py", 'root = "." + "loopx"', 1),
        ("loopx/probe.py", 'root = (("." + "co") + "dex") + "/skills"', 1),
        ("apps/probe.ts", r'const root = join(home, "\x2ecodex")', 1),
        ("apps/probe.ts", r'const root = "\u002eloopx"', 1),
        ("apps/probe.ts", r'const root = "\u{2e}codex"', 1),
        ("apps/probe.ts", r'const root = "\uD800/.codex"', 1),
        ("apps/probe.ts", 'const root = ("." + "lo") + "opx"', 1),
        ("apps/probe.ts", 'const root = "." /* part */ + "codex"', 1),
        ("apps/probe.ts", 'const root = /"ignored"/.test(x) ? "\\x2ecodex" : "other"', 1),
        ("apps/probe.ts", r'const root = `${home}/\x2ecodex/skills`', 1),
        ("apps/probe.ts", 'const root = `${home}/${"." + "codex"}`', 1),
        ("scripts/probe.sh", r"root=$'\x2eloopx'", 1),
        ("scripts/probe.sh", r"root=$'\056codex'", 1),
        ("scripts/probe.sh", r"printf %s $'\cA.codex'", 1),
        ("scripts/probe.sh", r"root=$'\u002eloopx'", 1),
        ("scripts/probe.sh", r"root=$'\u2e'codex", 1),
        ("scripts/probe.sh", r"root=$'\U0000002ecodex'", 1),
        ("scripts/probe.zsh", r"root=$'\U2e'loopx", 1),
        ("scripts/probe.sh", r"root=$'\x2e'loopx/goals", 1),
        ("scripts/probe.sh", "root='.'\"codex\"/skills", 1),
        ("scripts/probe.sh", 'root=$name".codex"', 1),
        ("scripts/probe.sh", 'root=".codex"$name', 1),
        ("scripts/probe.sh", 'root="${name}"".codex"', 1),
        ("scripts/probe.sh", r'root=.\codex', 1),
        ("scripts/probe.ps1", 'Join-Path $HOME "`u{2e}codex"', 1),
        ("scripts/probe.ps1", '$root = $prefix + ".codex"', 1),
        ("scripts/probe.ps1", '$root = "${prefix}" + ".codex"', 1),
        ("scripts/probe.ps1", '$root = [string]::Empty + ".codex"', 1),
        ("scripts/probe.ps1", 'Write-Output foo"/`u{2e}codex"', 1),
        ("scripts/probe.ps1", '$root = "``$prefix" + ".codex"', 1),
        ("scripts/probe.ps1", 'Join-Path $HOME .codex', 1),
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
        ("scripts/probe.sh", 'root=$HOME/foo#bar/.codex', 1),
        ("scripts/probe.bash", 'root=$HOME/foo#bar/.codex', 1),
        ("scripts/probe.zsh", 'root=$HOME/foo#bar/.codex', 1),
        ("scripts/probe.sh", r'root=$HOME/foo\#bar/.codex', 1),
        ("scripts/probe.sh", 'root="$HOME/foo#bar/.codex" # .loopx', 1),
        ("scripts/probe.sh", "root='foo#bar/.codex'", 1),
        ("scripts/probe.sh", 'root="foo"#bar/.codex', 1),
        ("scripts/probe.ps1", 'Write-Output foo#bar/.codex', 1),
        ("scripts/probe.ps1", 'Write-Output foo<#/.codex#>', 1),
        ("scripts/probe.ps1", 'Write-Output foo"bar"#suffix/.codex', 1),
        ("scripts/probe.ps1", "Write-Output foo'bar'#suffix/.codex", 1),
        ("scripts/probe.ps1", 'Write-Output foo=bar"baz"#suffix/.codex', 1),
        ("scripts/probe.ps1", '$root=".codex"# .loopx', 1),
        ("scripts/probe.ps1", 'Write-Output `#bar/.codex', 1),
        ("scripts/probe.ps1", '$root = "$HOME/foo#bar/.codex" # .loopx', 1),
        ("scripts/probe.ps1", "$root = 'foo#bar/.codex'", 1),
        ("scripts/probe.ps1", '$root = "<# literal .codex #>"', 1),
        ("scripts/probe.ps1", '$root = "quoted `"# .codex`""', 1),
        ("scripts/probe.ps1", "$root = 'quoted ''# .codex'''", 1),
        ("scripts/probe.ps1", '<# ".loopx" #>\n$root = ".codex"\n<# ".codex" #>', 1),
        ("scripts/probe.zsh", r"root=$'\x.codex'", 1),
        ("scripts/probe.zsh", r"root=$'\u.codex'", 1),
        ("scripts/probe.zsh", r"root=$'\U.codex'", 1),
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
        ("loopx/probe.py", 'name = "." + "loopx-python"'),
        ("loopx/probe.py", 'name = "." + variable + "codex"'),
        ("apps/probe.ts", r'const name = "\\x2ecodex"'),
        ("apps/probe.ts", r'const name = String.raw`\x2ecodex`'),
        ("apps/probe.ts", r'const name = "\x2eloopx-python"'),
        ("apps/probe.ts", 'const name = "." + variable + "codex"'),
        ("apps/probe.ts", 'const name = "."; const other = "codex"'),
        ("scripts/probe.sh", r"root='\x2eloopx'"),
        ("scripts/probe.sh", r'root="\x2ecodex"'),
        ("scripts/probe.sh", r"root=$'\\x2ecodex'"),
        ("scripts/probe.sh", r"printf %s $'\\cA.codex'"),
        ("scripts/probe.zsh", r"printf %s $'\cA.codex'"),
        ("scripts/probe.sh", "#!/usr/bin/env zsh\n" + r"printf %s $'\cA.codex'"),
        ("scripts/probe.sh", r"root=$'\x2eloopx-python'"),
        ("scripts/probe.sh", "printf '%s' '.' 'codex'"),
        ("scripts/probe.sh", r"root=$'\0.codex'"),
        ("scripts/probe.sh", r"root=$'\x00.codex'"),
        ("scripts/probe.sh", r"root=$'\c@.codex'"),
        ("scripts/probe.sh", "root='.'$name'codex'"),
        ("scripts/probe.ps1", "Join-Path $HOME '`u{2e}codex'"),
        ("scripts/probe.ps1", 'Join-Path $HOME "``u{2e}codex"'),
        ("scripts/probe.ps1", 'name = "." + "loopx-python"'),
        ("scripts/probe.ps1", 'name = "." + $variable + "codex"'),
        ("scripts/probe.ps1", '$root = "." + "codex".Length'),
        ("scripts/probe.ps1", '$root = "." + "codex"<# note #>.Length'),
        ("scripts/probe.ps1", '$root = "." + "codex"[0]'),
        ("scripts/probe.ps1", '$root = "." + "codex".Substring(0, 1)'),
        ("scripts/probe.ps1", '$root = "." + "codex" * 0'),
        ("scripts/probe.ps1", '$root = ".".Trim() + "codex"'),
        ("scripts/probe.ps1", '$root = "." + ("codex").Length'),
        ("scripts/probe.ps1", 'Write-Output "." + "codex"'),
        ("scripts/probe.ps1", 'Write-Output "."+"codex"'),
        ("apps/probe.ts", '// "~/.loopx"\n/* ".codex/skills" */\nconst x = "loopx"'),
        ("scripts/probe.sh", '# root=$HOME/.loopx\nroot="default" # .codex/skills'),
        ("scripts/probe.sh", 'root=default;# ".codex"\n# .loopx'),
        ("scripts/probe.sh", 'true &&# .codex\ntrue |# .loopx'),
        ("scripts/probe.ps1", '<#\nUses ".codex" and ".loopx/goals".\n#>\n$root = "default"'),
        ("scripts/probe.ps1", '$root = "default"<#\nUses ".codex".\n#>'),
        ("scripts/probe.ps1", '$root = "default"#.codex\n# .loopx'),
        ("scripts/probe.ps1", '$root = "default";# .codex'),
        ("scripts/probe.ps1", '# <#\n# ".codex"\n$root = "default"'),
        ("scripts/probe.ps1", '<# first #><#\n".codex"\n#>\n$root = "default"'),
        ("scripts/probe.ps1", '<# first #># .codex'),
        ("scripts/probe.ps1", 'Write-Output foo"bar" #suffix/.codex'),
        ("scripts/probe.ps1", "Write-Output foo'bar' #suffix/.codex"),
        ("scripts/probe.ps1", '$root="default"# .codex'),
        ("scripts/probe.ps1", '$root ="default"# .codex'),
        ("scripts/probe.ps1", 'Write-Output "default",# .codex'),
        ("scripts/probe.sh", r"root=$'\xc3\xa9.codex'"),
        ("scripts/probe.zsh", r"root=$'\xc3\xa9.codex'"),
        ("scripts/probe.sh", r"root=$'\xc3'$'\xa9.codex'"),
        ("scripts/probe.zsh", r"root=$'\xc3'$'\xa9.codex'"),
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
        "docs/probe.py", "README.md", "loopx/paths.py",
    ],
)
def test_detector_excludes_nonproduction_sources(path: str) -> None:
    assert _inventory({path: 'root = "~/.loopx"'}) == {}


@pytest.mark.parametrize("comment", ['<#\nUses ".loopx".\n#>', '<# first #><#\n".loopx"\n#>'])
def test_powershell_block_comment_preserves_literal_line_numbers(comment: str) -> None:
    source = comment + '\n$root = ".codex"'
    assert [
        line for line, literal in _source_literals("scripts/probe.ps1", source)
        if PATH_LITERAL.search(literal)
    ] == [4]



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


@pytest.mark.parametrize(
    ("path", "escaped", "plain", "changed"),
    [
        ("loopx/probe.py", 'root = "." + "loopx"', 'root = ".loopx"',
         'root = "." + "codex"'),
        ("apps/probe.ts", r'const root = "\x2eloopx"', 'const root = ".loopx"',
         'const root = "." + "codex"'),
        ("scripts/probe.sh", r"root=$'\x2eloopx'", 'root=".loopx"',
         "root='.'codex"),
        ("scripts/probe.ps1", 'root = "`u{2e}loopx"', 'root = ".loopx"',
         'root = "`u{2e}codex"'),
        ("apps/probe.ts", r'const root = "\ud800/.codex"',
         r'const root = "\uD800/.codex"', r'const root = "\uD801/.codex"'),
        ("scripts/probe.ps1", '$root = "a""/.codex"', '$root = "a`"/.codex"',
         '$root = "b""/.codex"'),
        ("scripts/probe.ps1", 'Write-Output foo"/`u{2e}codex"',
         'Write-Output foo"/.codex"', 'Write-Output foo"/`u{2e}loopx"'),
        ("scripts/probe.ps1", '$root = "`u{24}`u{2e}codex"', '$root = "`$.codex"',
         '$root = "`u{24}`u{2e}loopx"'),
        ("scripts/probe.sh", r'printf "%s" "\$."codex', r'printf "%s" "\$.codex"',
         r'printf "%s" "\$."loopx'),
        ("scripts/probe.sh", r"root=$'.\0'codex", 'root=".codex"',
         r"root=$'.\0'loopx"),
        ("scripts/probe.sh", r"root=$'.\x00'codex", 'root=".codex"',
         r"root=$'.\x00'loopx"),
        ("scripts/probe.sh", r"root=$'.\c@'codex", 'root=".codex"',
         r"root=$'.\c@'loopx"),
        ("scripts/probe.zsh", r"root=$'\.codex'", 'root=".codex"',
         r"root=$'\.loopx'"),
        ("scripts/probe.zsh", r"root=$'\0.codex'", r"root=$'\x00.codex'",
         r"root=$'\0.loopx'"),
        ("scripts/probe.sh", r"root=$'\?/.codex'", "root='?/.codex'",
         r"root=$'\?/.loopx'"),
        ("scripts/probe.zsh", r"root=$'\CA.codex'", r"root=$'\x01.codex'",
         r"root=$'\CA.loopx'"),
        ("scripts/probe.zsh", r"root=$'\C-A.codex'", r"root=$'\x01.codex'",
         r"root=$'\C-A.loopx'"),
        ("scripts/probe.sh", r"root=$'\xc3\xa9/.codex'", "root='é/.codex'",
         r"root=$'\xe9/.codex'"),
        ("scripts/probe.zsh", r"root=$'\xc3\xa9/.codex'", "root='é/.codex'",
         r"root=$'\xe9/.codex'"),
        ("scripts/probe.sh", r"root=$'\303\251/.codex'", "root='é/.codex'",
         r"root=$'\351/.codex'"),
        ("scripts/probe.zsh", r"root=$'\303\251/.codex'", "root='é/.codex'",
         r"root=$'\351/.codex'"),
        ("scripts/probe.sh", r"root=$'\xc3'$'\xa9/.codex'", "root='é/.codex'",
         r"root=$'\xe9/.codex'"),
        ("scripts/probe.zsh", r"root=$'\xc3'$'\xa9/.codex'", "root='é/.codex'",
         r"root=$'\xe9/.codex'"),
        ("scripts/probe.sh", "root=$'\\cä/.codex'", r"root=$'\x03\xa4/.codex'",
         "root='\\cä/.codex'"),
        ("scripts/probe.sh", "root=$'\\c字/.codex'", r"root=$'\x05\xad\x97/.codex'",
         "root='\\c字/.codex'"),
        ("scripts/probe.zsh", r"root=$'\x/.codex'", r"root=$'\0/.codex'",
         "root='x/.codex'"),
        ("scripts/probe.zsh", r"root=$'\u/.codex'", r"root=$'\0/.codex'",
         "root='u/.codex'"),
        ("scripts/probe.zsh", r"root=$'\U/.codex'", r"root=$'\0/.codex'",
         "root='U/.codex'"),
        ("scripts/probe.zsh", r"root=$'\U00110000/.codex'",
         r"root=$'\xf4\x90\x80\x80/.codex'", r"root='\U00110000/.codex'"),
        ("scripts/probe.sh", r"root=$'\c'/.codex", r"root='\/.codex'",
         r"root='\c/.codex'"),
        ("scripts/probe.zsh", r"root=$'.\C'codex", 'root=".codex"',
         r"root=$'.\C'loopx"),
        ("scripts/probe.zsh", r"root=$'.\M-'codex", 'root=".codex"',
         r"root=$'.\M-'loopx"),
        ("scripts/probe.zsh", r"root=$'\C-\u0041/.codex'", r"root=$'A\x0f.codex'",
         "root='A/.codex'"),
        ("scripts/probe.zsh", r"root=$'\M-\u0041/.codex'", r"root=$'A\xaf.codex'",
         "root='A/.codex'"),
        ("scripts/probe.zsh", r"root=$'\C-\C-?/.codex'", r"root=$'\x7f/.codex'",
         r"root=$'\x1f/.codex'"),
    ],
)
def test_decoded_budget_preserves_value_count_and_file_boundaries(
    path: str, escaped: str, plain: str, changed: str,
) -> None:
    budget = _inventory({path: plain})
    assert budget, "positive control must grant exactly one decoded value"
    assert sum(budget[path].values()) == 1
    assert _inventory({path: escaped}) == budget
    assert _regressions(_inventory({path: changed}), budget)
    assert _regressions(_inventory({path: escaped + "\n" + escaped}), budget)
    other = path.replace("probe", "other")
    assert _regressions(_inventory({other: escaped}), budget)


@pytest.mark.parametrize(
    ("escape", "codepoint"),
    [("a", "7"), ("b", "8"), ("e", "1b"), ("f", "c"), ("v", "b")],
)
def test_powershell_control_escapes_share_the_unicode_value_budget(
    escape: str, codepoint: str,
) -> None:
    path = "scripts/probe.ps1"
    budget = _inventory({path: f'Join-Path $HOME "`u{{{codepoint}}}.codex"'})
    assert sum(budget[path].values()) == 1
    assert _inventory({path: f'Join-Path $HOME "`{escape}.codex"'}) == budget


@pytest.mark.parametrize("operator", [";", "|cat", "&&true", "||true", ">output"])
def test_shell_command_operators_do_not_change_literal_value_budget(operator: str) -> None:
    path = "scripts/probe.sh"
    source = 'printf "%s" ".codex"'
    budget = _inventory({path: source})
    assert sum(budget[path].values()) == 1
    assert _inventory({path: source + operator}) == budget


@pytest.mark.parametrize("source", [
    '$root = "." + "codex"',
    '$root = "." + <# note #> "codex"',
    '$root = "." <# note #> + "codex"',
    '$root = ("." + "codex").Length',
    'Join-Path $HOME ("." + "loopx")',
])
def test_powershell_literals_do_not_interpret_expressions(source: str) -> None:
    assert _inventory({"scripts/probe.ps1": source}) == {}


@pytest.mark.parametrize(
    ("bash_controls", "raw", "expected"),
    [
        (True, "\\\n", "\\\n"),
        (False, "\\\n", "\n"),
        (True, r"\?", "?"),
        (False, r"\C?", "\x7f"),
        (False, r"\C-?", "\x7f"),
        (False, r"\C_", "\x1f"),
        (False, r"\C-1", "\x11"),
        (False, r"\M-a", "\xe1"),
        (False, r"\M-\C-A", "\x81"),
        (False, r"\C-\M-A", "\x81"),
        (False, r"\M-\C-?", "\xff"),
        (False, r"\C-\M-?", "\x9f"),
        (False, r"\M-\x2e", "\xae"),
        (False, r"\C-\x41", "\x01"),
        (False, r"\M-\n", "\x8a"),
        (False, r"\C-\n", "\n"),
    ],
)
def test_ansi_literal_preserves_native_escape_values(
    bash_controls: bool, raw: str, expected: str,
) -> None:
    actual = _ansi_literal(raw, bash_controls=bash_controls)
    assert actual.encode("utf-8", errors="surrogateescape") == expected.encode("latin1")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(r"\C-\u0041x", b"A\x18"), (r"\M-\u0041x", b"A\xf8"),
     (r"\C-\C-?", b"\x7f"), (r"\C-\M-\C-?", b"\x9f"),
     (r"\M-\C-\C-?", b"\xff"), (r"\M-\M-\C-?", b"\xff"),
     (r"\C-\M-\M-?", b"\x9f"), (r"\C-\u0041\u0042x", b"AB\x18")],
)
def test_zsh_control_state_survives_unicode_and_repeated_modifiers(
    raw: str, expected: bytes,
) -> None:
    value = _ansi_literal(raw, bash_controls=False)
    assert value.encode("utf-8", errors="surrogateescape") == expected


@pytest.mark.parametrize("raw", [r"\C", r"\C-", r"\M", r"\M-", r"\C-\M-", r"\M-\C-"])
def test_zsh_control_prefix_without_operand_is_empty(raw: str) -> None:
    assert _ansi_literal(raw, bash_controls=False) == ""


def test_bash_control_prefix_without_operand_retains_backslash() -> None:
    assert _ansi_literal(r"\c", bash_controls=True) == "\\"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(r"\x", b"\0"), (r"\u", b"\0"), (r"\U", b"\0"),
     (r"\U00110000", b"\xf4\x90\x80\x80"),
     (r"\U001fffff", b"\xf7\xbf\xbf\xbf"),
     (r"\U00200000", b"\xf8\x88\x80\x80\x80"),
     (r"\U03ffffff", b"\xfb\xbf\xbf\xbf\xbf"),
     (r"\U04000000", b"\xfc\x84\x80\x80\x80\x80"),
     (r"\U7fffffff", b"\xfd\xbf\xbf\xbf\xbf\xbf"),
     (r"\U80000000", b"\xfe\x80\x80\x80\x80\x80"),
     (r"\Uffffffff", b"\xff\xbf\xbf\xbf\xbf\xbf")],
)
def test_zsh_numeric_escapes_preserve_native_bytes(raw: str, expected: bytes) -> None:
    value = _ansi_literal(raw, bash_controls=False)
    assert value.encode("utf-8", errors="surrogateescape") == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("\\cä", b"\x03\xa4"), ("\\c字", b"\x05\xad\x97")],
)
def test_bash_controls_preserve_utf8_continuation_bytes(raw: str, expected: bytes) -> None:
    value = _ansi_literal(raw, bash_controls=True)
    assert value.encode("utf-8", errors="surrogateescape") == expected


@pytest.mark.parametrize(
    ("escaped", "plain"),
    [(r"\C-\u0041", "A"), (r"\C-\u00e9", "é"),
     (r"\M-\u0041", "A"), (r"\C-\U00000041", "A")],
)
def test_zsh_unicode_escapes_ignore_control_modifiers(escaped: str, plain: str) -> None:
    assert _ansi_literal(escaped, bash_controls=False) == plain


@pytest.mark.parametrize("suffix", ["sh", "zsh"])
@pytest.mark.parametrize("escaped", [r"\xe9", r"\351"])
def test_shell_byte_literals_cannot_borrow_a_unicode_value_budget(
    suffix: str, escaped: str,
) -> None:
    path = f"scripts/probe.{suffix}"
    budget = _inventory({path: "root='é/.codex'"})
    changed = _inventory({path: f"root=$'{escaped}/.codex'"})
    assert sum(budget[path].values()) == sum(changed[path].values()) == 1
    assert _regressions(changed, budget)


@pytest.mark.parametrize("suffix", ["sh", "zsh"])
def test_ansi_literal_newline_does_not_join_path_fragments(suffix: str) -> None:
    source = "printf %s $'.\\\n'codex"
    assert _inventory({f"scripts/probe.{suffix}": source}) == {}


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
    assert payload["schema_version"] == 2
    budget = {path: Counter(rows) for path, rows in payload["files"].items()}
    current = _repository_inventory(REPOSITORY_ROOT)
    assert _tracked_sources(REPOSITORY_ROOT), "production-source scan must not be empty"
    excess = _regressions(current, budget)
    details = []
    for path, fingerprints in excess.items():
        source = (REPOSITORY_ROOT / path).read_text()
        lines = [
            str(line) for line, literal in _source_literals(path, source)
            if _fingerprint(literal) in fingerprints
        ]
        details.append(f"{path}:{','.join(lines)} (+{sum(fingerprints.values())})")
    assert not excess, (
        "New raw path literals bypass loopx.paths; use its helpers rather than "
        "raising the residual budget: " + "; ".join(details)
    )
