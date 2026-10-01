from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from loopx.capabilities.benchmark_toolkit.native_codex_profile import (
    NativeCodexGoalPrompt,
    NativeCodexProfile,
    NativeCodexProfileError,
    compact_native_codex_goal_prompt_receipt,
    compact_native_codex_profile_receipt,
    inspect_native_codex_profile,
    install_native_codex_profile,
    native_codex_app_server_shell_policy_args,
    native_codex_profile_environment,
    render_native_codex_goal_prompt,
)
from loopx.cli import build_parser


REPO_ROOT = Path(__file__).resolve().parents[2]


def _embedded_product_bootstrap_source() -> str:
    adapter = REPO_ROOT / "benchmark/deepswe-gptxhigh-v1/loopx_native_codex.py"
    tree = ast.parse(adapter.read_text(encoding="utf-8"), filename=str(adapter))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "_BOOTSTRAP"
            for target in node.targets
        ):
            source = ast.literal_eval(node.value)
            if isinstance(source, str):
                return source
    raise AssertionError("native Codex adapter has no literal _BOOTSTRAP source")


def test_benchmark_preflight_tracks_the_current_bootstrap_contract() -> None:
    preflight = (
        REPO_ROOT / "benchmark/deepswe-gptxhigh-v1/preflight_loopx_rerun.py"
    ).read_text(encoding="utf-8")

    assert '"--write-scope", a.project' in preflight
    for retired_flag in (
        "--codex-app-heartbeat",
        "--no-onboarding-scan",
        "--begin-autonomous-advance",
    ):
        assert retired_flag not in preflight


@pytest.mark.parametrize("wen_compat", [True, False])
def test_embedded_product_bootstrap_emits_only_current_cli_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wen_compat: bool,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task_file = tmp_path / "task.txt"
    task_file.write_text("Repair the fixture.", encoding="utf-8")
    parser = build_parser()
    parsed_commands: list[str] = []

    def parse_without_executing(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        arguments = command[1:]
        parsed = parser.parse_args(arguments)
        parsed_commands.append(str(parsed.command))
        payload: dict[str, object] = {"ok": True}
        if parsed.command == "heartbeat-prompt":
            payload["task_body"] = "Use the current LoopX contract."
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", parse_without_executing)
    monkeypatch.setenv("LOOPX_WEN_COMPAT", "1" if wen_compat else "0")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "loopx_product_bootstrap.py",
            "--profile-root",
            str(tmp_path / "profile"),
            "--project",
            str(project),
            "--goal-id",
            "benchmark-goal",
            "--agent-id",
            "benchmark-agent",
            "--goal-doc-file",
            str(tmp_path / "goal.md"),
            "--task-file",
            str(task_file),
            "--runtime-profile",
            "codex_cli",
            "--objective-out",
            str(tmp_path / "objective.txt"),
            "--receipt-out",
            str(tmp_path / "receipt.json"),
        ],
    )

    source = _embedded_product_bootstrap_source()
    with pytest.raises(SystemExit) as stopped:
        exec(compile(source, "<loopx_product_bootstrap>", "exec"), {})

    assert stopped.value.code == 0
    expected_commands = ["bootstrap", "configure-goal", "todo"]
    if wen_compat:
        expected_commands.append("configure-goal")
    expected_commands.append("heartbeat-prompt")
    assert parsed_commands == expected_commands


def _fake_profile(tmp_path: Path, *, bind_cli: bool = True) -> NativeCodexProfile:
    root = tmp_path / "profile"
    cli = root / "bin" / "loopx"
    cli.parent.mkdir(parents=True)
    task_body = (
        'f"Use {cli} with runtime root {runtime}."'
        if bind_cli
        else '"No installed CLI reference."'
    )
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "cli = sys.argv[sys.argv.index('--cli-bin') + 1]\n"
        "runtime = sys.argv[sys.argv.index('--runtime-root') + 1]\n"
        f"task_body = {task_body}\n"
        "print(json.dumps({\n"
        "    'ok': True,\n"
        "    'runtime_profile': 'codex_cli',\n"
        "    'interface_budget': {'within_budget': True},\n"
        "    'task_body': task_body,\n"
        "}))\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    return NativeCodexProfile(
        root=root,
        home=root / "home",
        codex_home=root / "codex-home",
        skills_dir=root / "codex-home/skills",
        bin_dir=root / "bin",
        cli_bin=cli,
        release_root=root / "releases/native-goal-profile",
        source_revision="a" * 40,
        source_clean=True,
        skills_digest="b" * 64,
        required_skill_ids=("loopx", "loopx-project"),
        materialized_skill_ids=("loopx", "loopx-project"),
    )


def test_compact_profile_receipt_excludes_local_paths() -> None:
    private = Path("/private/benchmark/profile")
    profile = NativeCodexProfile(
        root=private,
        home=private / "home",
        codex_home=private / "codex-home",
        skills_dir=private / "codex-home/skills",
        bin_dir=private / "bin",
        cli_bin=private / "bin/loopx",
        release_root=private / "releases/native-goal-profile",
        source_revision="a" * 40,
        source_clean=True,
        skills_digest="b" * 64,
        required_skill_ids=("loopx", "loopx-project"),
        materialized_skill_ids=("loopx", "loopx-project"),
    )

    receipt = compact_native_codex_profile_receipt(profile)
    rendered = json.dumps(receipt, sort_keys=True)

    assert receipt["source_clean"] is True
    assert receipt["skill_readback_ready"] is True
    assert "/private" not in rendered


def test_profile_environment_excludes_ambient_provider_values(tmp_path: Path) -> None:
    profile = _fake_profile(tmp_path)
    base_env = {
        "PATH": "/usr/bin:/bin",
        "CODEX_GOAL_API_KEY": "provider-value",
        "UNRELATED_PRIVATE_VALUE": "must-not-pass",
    }
    profile_env = native_codex_profile_environment(profile, base_env=base_env)

    assert "CODEX_GOAL_API_KEY" not in profile_env
    assert "UNRELATED_PRIVATE_VALUE" not in profile_env
    assert profile_env["CODEX_HOME"] == str(profile.codex_home)


def test_native_codex_app_server_shell_policy_is_explicit_and_fail_closed() -> None:
    args = native_codex_app_server_shell_policy_args(
        excluded_env_keys=("SECOND_PROVIDER_KEY", "PRIMARY_PROVIDER_KEY"),
    )
    assert args == (
        "-c",
        'shell_environment_policy.inherit="core"',
        "-c",
        "shell_environment_policy.ignore_default_excludes=false",
        "-c",
        (
            'shell_environment_policy.include_only=["HOME", "LANG", "LC_ALL", '
            '"LC_CTYPE", "LOGNAME", "PATH", "SHELL", "SSL_CERT_DIR", '
            '"SSL_CERT_FILE", "TERM", "TMPDIR", "TZ", "USER"]'
        ),
        "-c",
        'shell_environment_policy.exclude=["PRIMARY_PROVIDER_KEY", "SECOND_PROVIDER_KEY"]',
    )


@pytest.mark.parametrize("excluded_env_key", ("", "invalid-key", "WITH SPACE"))
def test_native_codex_app_server_shell_policy_rejects_unsafe_keys(
    excluded_env_key: str,
) -> None:
    with pytest.raises(ValueError, match="excluded_env_keys"):
        native_codex_app_server_shell_policy_args(
            excluded_env_keys=(excluded_env_key,),
        )


def test_installed_cli_renders_and_binds_the_real_goal_prompt(tmp_path: Path) -> None:
    profile = _fake_profile(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    runtime_registry = project / ".loopx/runtime/registry.global.json"

    prompt = render_native_codex_goal_prompt(
        profile,
        project_root=project,
        goal_id="goal-1",
        agent_id="agent-1",
        runtime_registry_path=runtime_registry,
        base_env={"PATH": "/usr/bin:/bin"},
    )

    assert isinstance(prompt, NativeCodexGoalPrompt)
    assert str(profile.cli_bin) in prompt.task_body
    assert str((project / ".loopx/runtime").resolve()) in prompt.task_body
    assert "$HOME/.codex/loopx/registry.global.json" not in prompt.task_body
    receipt = compact_native_codex_goal_prompt_receipt(prompt)
    rendered = json.dumps(receipt, sort_keys=True)
    assert receipt["installed_cli_bound"] is True
    assert receipt["runtime_registry_bound"] is True
    assert str(tmp_path) not in rendered


def test_goal_prompt_fails_when_output_does_not_bind_installed_cli(
    tmp_path: Path,
) -> None:
    profile = _fake_profile(tmp_path, bind_cli=False)
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(
        NativeCodexProfileError,
        match="goal_prompt_installed_cli_not_bound",
    ):
        render_native_codex_goal_prompt(
            profile,
            project_root=project,
            goal_id="goal-1",
            agent_id="agent-1",
            base_env={"PATH": "/usr/bin:/bin"},
        )


def test_profile_inspection_fails_closed_on_missing_install(tmp_path: Path) -> None:
    with pytest.raises(NativeCodexProfileError, match="formal_install_outputs_missing"):
        inspect_native_codex_profile(tmp_path)


def test_profile_install_requires_shipped_installer(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()

    with pytest.raises(NativeCodexProfileError, match="formal_installer_missing"):
        install_native_codex_profile(source, tmp_path / "profile")


def test_profile_install_fails_before_target_when_cleanliness_is_unproven(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    installer = source / "scripts" / "install-local.sh"
    installer.parent.mkdir(parents=True)
    installer.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    profile = tmp_path / "profile"

    with pytest.raises(
        NativeCodexProfileError,
        match="profile_source_cleanliness_unproven",
    ):
        install_native_codex_profile(source, profile)

    assert not profile.exists()


def test_profile_install_rejects_a_file_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    installer = source / "scripts" / "install-local.sh"
    installer.parent.mkdir(parents=True)
    installer.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    profile = tmp_path / "profile"
    profile.write_text("occupied", encoding="utf-8")

    with pytest.raises(NativeCodexProfileError, match="profile_root_not_empty"):
        install_native_codex_profile(
            source,
            profile,
            require_clean_source=False,
        )
