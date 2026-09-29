from __future__ import annotations

import json

import pytest

from loopx.cli import main
from loopx.configure_goal import configure_goal
from loopx.workspace.repos import goal_repos, merge_goal_repos, parse_repo_spec

GOAL = "wsgoal"


def test_parse_repo_spec_and_legacy_fallback(tmp_path):
    entry = parse_repo_spec(f"web={tmp_path},default_branch=dev,merge_target=task_branch,task_branch=int/x")
    assert entry == {
        "name": "web",
        "path": str(tmp_path),
        "default_branch": "dev",
        "merge_target": "task_branch",
        "task_branch": "int/x",
    }
    with pytest.raises(ValueError, match="absolute"):
        parse_repo_spec("web=relative/path")
    with pytest.raises(ValueError, match="merge_target"):
        parse_repo_spec(f"web={tmp_path},merge_target=prod")
    with pytest.raises(ValueError, match="must be one of"):
        parse_repo_spec(f"web={tmp_path},colour=red")
    with pytest.raises(ValueError, match="duplicate"):
        merge_goal_repos([], [entry, entry])
    legacy = goal_repos({"id": "g", "repo": str(tmp_path)})
    assert legacy == [
        {"name": "main", "path": str(tmp_path), "default_branch": None, "merge_target": "main", "legacy": True}
    ]
    assert goal_repos({"id": "g"}) == []
    declared = goal_repos({"id": "g", "repo": "/ignored", "repos": [{"name": "a", "path": str(tmp_path)}]})
    assert [repo["name"] for repo in declared] == ["a"]


def test_configure_goal_upserts_repos_and_cli_writes_them(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "loopx.control_plane.runtime.runtime_projection_route.DEFAULT_RUNTIME_ROOT",
        tmp_path / "default-runtime",
    )
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "common_runtime_root": str(tmp_path / "runtime"),
                "goals": [{"id": GOAL, "repo": str(tmp_path), "status": "active"}],
            }
        )
    )
    preview = configure_goal(
        registry_path=registry, goal_id=GOAL, repos=[parse_repo_spec(f"api={tmp_path}/api")]
    )
    assert preview["changed_fields"] == ["repos"] and preview["written"] is False
    assert "repos" not in json.loads(registry.read_text())["goals"][0]
    code = main(
        [
            "--registry", str(registry), "--format", "json", "configure-goal", "--goal-id", GOAL,
            "--repo", f"api={tmp_path}/api", "--repo", f"web={tmp_path}/web,merge_target=task_branch",
            "--execute",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    stored = json.loads(registry.read_text())["goals"][0]
    assert [repo["name"] for repo in stored["repos"]] == ["api", "web"]
    assert stored["repo"] == str(tmp_path)  # legacy field untouched
    configure_goal(
        registry_path=registry,
        goal_id=GOAL,
        repos=[parse_repo_spec(f"api={tmp_path}/api2,default_branch=trunk")],
        execute=True,
    )
    stored = json.loads(registry.read_text())["goals"][0]
    assert stored["repos"][0] == {
        "name": "api", "path": f"{tmp_path}/api2", "default_branch": "trunk", "merge_target": "main",
    }
    assert [repo["name"] for repo in stored["repos"]] == ["api", "web"]
    configure_goal(registry_path=registry, goal_id=GOAL, clear_repos=True, execute=True)
    assert "repos" not in json.loads(registry.read_text())["goals"][0]


