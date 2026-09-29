"""The test session refuses access to the real user LoopX roots.

These tests exercise the guard against temporary stand-ins for the real
``~/.codex/loopx`` and ``~/.loopx``, so a broken guard can never touch the
owner's live state.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import real_runtime_root_guard as guard

from loopx.file_lock import try_exclusive_file_lock
from loopx.paths import DEFAULT_RUNTIME_ROOT

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD_DIR = Path(guard.__file__).resolve().parent


def _stand_in(tmp_path: Path) -> Path:
    return tmp_path / "real-home" / ".codex" / "loopx"


def test_session_protects_the_real_loopx_roots_whatever_home_a_test_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    homes = [Path.home()]
    with contextlib.suppress(ImportError, KeyError):
        import pwd

        homes.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
    real_roots = {os.path.normpath(DEFAULT_RUNTIME_ROOT)}
    for home in homes:
        real_roots.add(os.path.normpath(home / ".codex" / "loopx"))
        real_roots.add(os.path.normpath(home / ".loopx"))
    assert real_roots <= set(guard.protected_roots())

    monkeypatch.setenv("HOME", str(tmp_path))

    assert real_roots <= set(guard.protected_roots())
    assert not any(root.startswith(str(tmp_path)) for root in guard.protected_roots())


def test_a_test_owned_home_keeps_a_usable_runtime_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    goal_dir = Path.home() / ".codex" / "loopx" / "goals" / "guard-probe"

    with guard.protecting(_stand_in(tmp_path), tmp_path / "report.jsonl"):
        goal_dir.mkdir(parents=True)
        (goal_dir / "state.json").write_text("{}", encoding="utf-8")
        listing = [path.name for path in goal_dir.parent.iterdir()]
        charged = guard.take_violations(request.node.nodeid)

    assert listing == ["guard-probe"]
    assert charged == ([], [])


def test_in_process_access_to_the_protected_root_is_refused_and_named(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    stand_in = _stand_in(tmp_path)
    (stand_in / "goals").mkdir(parents=True)
    (stand_in / "registry.global.json").write_text("{}", encoding="utf-8")

    with guard.protecting(stand_in, tmp_path / "report.jsonl"):
        with pytest.raises(PermissionError, match="real LoopX root"):
            (stand_in / "goals" / "probe").mkdir()
        with pytest.raises(PermissionError):
            (stand_in / "goals" / "probe.json").write_text("{}", encoding="utf-8")
        with pytest.raises(PermissionError):
            (stand_in / "registry.global.json").read_text(encoding="utf-8")
        with pytest.raises(PermissionError):
            list((stand_in / "goals").iterdir())
        own, others = guard.take_violations(request.node.nodeid)
        again = guard.take_violations(request.node.nodeid)

    events = [violation["event"] for violation in own]
    assert events[:3] == ["os.mkdir", "open", "open"]
    assert events[3:] in (["os.listdir"], ["os.scandir"])  # Path.iterdir differs by Python version
    assert others == []
    assert again == ([], [])
    assert list((stand_in / "goals").iterdir()) == []
    message = guard.describe(request.node.nodeid, own)
    assert message.startswith(f"{request.node.nodeid} used a real LoopX root")
    assert f"os.mkdir {stand_in / 'goals' / 'probe'} (this test process)" in message


def test_a_machine_scoped_lease_under_the_home_loopx_directory_is_refused(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    stand_in = tmp_path / "real-home" / ".loopx"
    lease = stand_in / "lark-consumers" / ("0" * 32)
    lease.parent.mkdir(parents=True)

    with guard.protecting(stand_in, tmp_path / "report.jsonl"):
        with pytest.raises(PermissionError, match="real LoopX root"):
            with try_exclusive_file_lock(lease, operation="lark_event_consumer"):
                pass
        own, _others = guard.take_violations(request.node.nodeid)

    assert ("open", f"{lease}.lock") in [(violation["event"], violation["path"]) for violation in own]
    assert list(lease.parent.iterdir()) == []


def test_a_protected_root_is_also_pinned_in_its_real_form(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    real_home = tmp_path / "real-home"
    (real_home / ".codex" / "loopx").mkdir(parents=True)
    link_home = tmp_path / "link-home"
    link_home.symlink_to(real_home, target_is_directory=True)
    physical = real_home / ".codex" / "loopx" / "probe"

    with guard.protecting(link_home / ".codex" / "loopx", tmp_path / "report.jsonl"):
        with pytest.raises(PermissionError):
            physical.mkdir()
        own, _others = guard.take_violations(request.node.nodeid)

    assert [(violation["event"], violation["path"]) for violation in own] == [
        ("os.mkdir", str(physical))
    ]
    assert not physical.exists()


def test_a_symlink_is_checked_at_its_location_not_at_its_target(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    stand_in = _stand_in(tmp_path)
    stand_in.mkdir(parents=True)
    outside_link = tmp_path / "outside-link"
    inside_link = stand_in / "inside-link"

    with guard.protecting(stand_in, tmp_path / "report.jsonl"):
        outside_link.symlink_to(stand_in / "registry.global.json")
        with pytest.raises(PermissionError):
            inside_link.symlink_to(tmp_path / "elsewhere")
        own, _others = guard.take_violations(request.node.nodeid)

    assert outside_link.is_symlink()
    assert not inside_link.is_symlink()
    assert [(violation["event"], violation["path"]) for violation in own] == [
        ("os.symlink", str(inside_link))
    ]


def test_a_refusal_recorded_for_another_test_is_not_charged_to_this_one(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in = _stand_in(tmp_path)
    other_test = "tests/test_other_module.py::test_other"

    with guard.protecting(stand_in, tmp_path / "report.jsonl"):
        monkeypatch.setenv("PYTEST_CURRENT_TEST", f"{other_test} (call)")
        with pytest.raises(PermissionError):
            os.mkdir(stand_in)
        monkeypatch.delenv("PYTEST_CURRENT_TEST")
        with pytest.raises(PermissionError):
            os.mkdir(stand_in)
        own, others = guard.take_violations(request.node.nodeid)

    assert own == []
    assert [guard.record_test_id(record) for record in others] == [other_test, None]
    report = guard.describe_unattributed(others)
    assert f"recorded under {other_test}" in report
    assert "recorded under no test" in report


def test_a_cli_subprocess_resolving_the_default_root_is_refused_and_reported(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    real_home = tmp_path / "real-home"
    stand_in = real_home / ".codex" / "loopx"
    probe = (
        "from loopx.paths import DEFAULT_RUNTIME_ROOT; "
        "(DEFAULT_RUNTIME_ROOT / 'goals' / 'guard-probe').mkdir(parents=True)"
    )

    with guard.protecting(stand_in, tmp_path / "report.jsonl"):
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=REPO_ROOT,
            env={**os.environ, "HOME": str(real_home)},
            capture_output=True,
            text=True,
            check=False,
        )
        own, others = guard.take_violations(request.node.nodeid)

    assert completed.returncode != 0
    assert "test guard refused os.mkdir" in completed.stderr
    assert not stand_in.exists()
    assert others == []
    assert [(violation["event"], violation["path"]) for violation in own] == [
        ("os.mkdir", str(stand_in / "goals" / "guard-probe"))
    ]
    assert own[0]["pid"] != os.getpid()


def test_a_guarded_subprocess_still_runs_the_sitecustomize_it_shadows(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    stand_in = _stand_in(tmp_path)
    other_site = tmp_path / "other-site"
    other_site.mkdir()
    marker = tmp_path / "marker.txt"
    (other_site / "sitecustomize.py").write_text(
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('loaded')\n",
        encoding="utf-8",
    )

    with guard.protecting(stand_in, tmp_path / "report.jsonl"):
        completed = subprocess.run(
            [sys.executable, "-c", "import os, sys; os.mkdir(sys.argv[1])", str(stand_in)],
            env={**os.environ, "PYTHONPATH": os.pathsep.join([str(GUARD_DIR), str(other_site)])},
            capture_output=True,
            text=True,
            check=False,
        )
        own, _others = guard.take_violations(request.node.nodeid)

    assert marker.read_text(encoding="utf-8") == "loaded"
    assert "test guard refused os.mkdir" in completed.stderr
    assert [violation["event"] for violation in own] == ["os.mkdir"]
    assert not stand_in.exists()


def test_a_subprocess_without_the_session_report_path_is_not_guarded(tmp_path: Path) -> None:
    stand_in = _stand_in(tmp_path)

    with guard.protecting(stand_in, tmp_path / "report.jsonl"):
        env = {**os.environ}
        env.pop(guard.REPORT_PATH_ENV)
        completed = subprocess.run(
            [sys.executable, "-c", "import os, sys; os.makedirs(sys.argv[1])", str(stand_in)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    assert completed.returncode == 0, completed.stderr
    assert stand_in.is_dir()


def test_a_session_reports_refusals_it_cannot_charge_to_a_running_test(tmp_path: Path) -> None:
    stand_in = _stand_in(tmp_path)
    (tmp_path / "test_inner.py").write_text(
        textwrap.dedent(
            f"""
            import os

            import pytest

            STAND_IN = {str(stand_in)!r}


            def test_refusal_recorded_for_another_test(monkeypatch):
                monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_other_module.py::test_other (call)")
                with pytest.raises(PermissionError):
                    os.mkdir(STAND_IN)


            def test_refusal_of_its_own():
                with pytest.raises(PermissionError):
                    os.mkdir(STAND_IN)
            """
        ),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        guard.PROTECTED_ROOTS_ENV: str(stand_in),
        "PYTHONPATH": os.pathsep.join(
            part for part in (str(GUARD_DIR), os.environ.get("PYTHONPATH")) if part
        ),
    }
    env.pop("PYTEST_ADDOPTS", None)

    completed = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "-p", "real_runtime_root_guard_plugin", "test_inner.py",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    output = completed.stdout
    assert completed.returncode == pytest.ExitCode.TESTS_FAILED, output + completed.stderr
    assert "2 passed, 1 error" in output
    assert "ERROR at teardown of test_refusal_of_its_own" in output
    assert "ERROR at teardown of test_refusal_recorded_for_another_test" not in output
    assert "recorded under tests/test_other_module.py::test_other" in output
    assert not stand_in.exists()


def test_a_subprocess_started_outside_the_checkout_imports_loopx_from_the_tested_tree(
    tmp_path: Path,
) -> None:
    import loopx

    # PathFinder searches sys.path only; an editable install resolves through a
    # meta-path finder after it and may point at another checkout.
    probe = (
        "import importlib.machinery, loopx; "
        "print(loopx.__file__); "
        "print(importlib.machinery.PathFinder.find_spec('loopx').origin)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    tested = REPO_ROOT / "loopx" / "__init__.py"
    assert Path(loopx.__file__).resolve() == tested
    assert completed.returncode == 0, completed.stderr
    assert [Path(line).resolve() for line in completed.stdout.splitlines()] == [tested, tested]
