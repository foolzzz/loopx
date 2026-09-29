"""The environment marker of an agent Turn's model process.

A LoopX Turn host exports ``LOOPX_AGENT_TURN=<agent id>`` to the subprocess
that runs the Turn's model (the claude-code, codex-cli and generic-cli host
commands and the dsh runtime), and only there: the dispatcher and the
``loopx turn run-once`` process that settles the Turn never set it. Every
LoopX command the model runs inherits it, so owner-only writes, such as a
user-gate decision, can refuse an agent Turn. This is a guardrail against
accidental self-approval, not a security boundary: the agent runs as the same
OS user and can unset the variable.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

AGENT_TURN_ENV_VAR = "LOOPX_AGENT_TURN"


def agent_turn_env(request: Mapping[str, Any]) -> dict[str, str]:
    """The marker to add to one Turn host subprocess's environment.

    The value is the Turn's agent id from its host request. The caller merges
    it into the child environment only; this process's own environment is
    never changed.
    """

    envelope = request.get("turn_envelope")
    agent_id = str(envelope.get("agent_id") or "").strip() if isinstance(envelope, Mapping) else ""
    return {AGENT_TURN_ENV_VAR: agent_id or "unknown"}


def agent_turn_marker(environ: Mapping[str, str] | None = None) -> str | None:
    """The agent id when this process runs inside an agent Turn, else None."""

    value = str((os.environ if environ is None else environ).get(AGENT_TURN_ENV_VAR) or "").strip()
    return value or None
