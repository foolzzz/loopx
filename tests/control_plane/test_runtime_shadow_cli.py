from __future__ import annotations

import pytest

from loopx.cli import build_parser


def test_removed_authority_shadow_command_is_rejected() -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["authority-shadow", "status", "--goal-id", "synthetic"])
    assert error.value.code == 2


@pytest.mark.parametrize("action", ["drain", "status"])
def test_runtime_shadow_recovery_uses_coordination_command(action: str) -> None:
    args = build_parser().parse_args(["coordination-shadow", action, "--goal-id", "synthetic"])
    assert args.coordination_shadow_command == action
    assert args.goal_id == "synthetic"
