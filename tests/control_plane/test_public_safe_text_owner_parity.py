"""One corpus, every real public-safe text owner.

The rule that decides whether control-plane text looks private is enforced by
four owners: `loopx.feedback`, `loopx.authority`, `loopx.boundary_authority`,
and the TypeScript Vision checkpoint reached through `build_vision_checkpoint`.
Testing one owner's helper cannot prove the contract, because the owners used
to disagree: the Vision path rejected ordinary "owner authorization" prose
while the Python patterns accepted a quoted-JSON credential header.

These tests drive the shared fixture through each owner's real entrypoint, so
any owner that drifts fails here instead of in a reviewer's manual probe.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from loopx.authority import validate_public_safe_text as validate_authority_text
from loopx.boundary_authority import build_checkpointed_boundary_authority_entry
from loopx.control_plane.goals.vision_checkpoint import build_vision_checkpoint
from loopx.feedback import validate_public_safe_text as validate_feedback_text

CORPUS_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "public_safe_text_corpus.json"
)
PLACEHOLDER_RE = re.compile(r"\{([A-Z][A-Z_]*)\}")


def _load_corpus() -> dict[str, Any]:
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    assert corpus["schema_version"] == "public_safe_text_corpus_v0"
    return corpus


def _render(template: str, tokens: dict[str, list[str]]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in tokens:
            return "".join(tokens[name])
        if name.endswith("_LOWER") and name[: -len("_LOWER")] in tokens:
            return "".join(tokens[name[: -len("_LOWER")]]).lower()
        raise AssertionError(f"corpus placeholder {name} has no token definition")

    return PLACEHOLDER_RE.sub(replace, template)


def _samples(group: str) -> list[tuple[str, str]]:
    corpus = _load_corpus()
    tokens = corpus["tokens"]
    return [
        (str(sample["id"]), _render(str(sample["template"]), tokens))
        for sample in corpus[group]
    ]


PUBLIC_SAFE_SAMPLES = _samples("public_safe")
PRIVATE_LOOKING_SAMPLES = _samples("private_looking")


def _ids(samples: list[tuple[str, str]]) -> list[str]:
    return [sample_id for sample_id, _ in samples]


def _boundary_authority_text(text: str) -> None:
    """Drive boundary_authority through its public builder, not its helper."""

    build_checkpointed_boundary_authority_entry(
        write_scopes=["goal_state"],
        source=text,
    )


def _vision_text(text: str) -> None:
    """Drive the real TypeScript Vision owner through the Python entrypoint."""

    build_vision_checkpoint(
        agent_id="fixture-cli",
        agent_vision=None,
        existing_agent_vision=None,
        vision_unchanged_reason=text,
        delivery_outcome="outcome_progress",
        active_state_next_action_update=None,
    )


OWNERS = (
    ("feedback", lambda text: validate_feedback_text("agent_vision.summary", text)),
    ("authority", lambda text: validate_authority_text("project_material.note", text)),
    ("boundary_authority", _boundary_authority_text),
    ("vision_checkpoint_ts", _vision_text),
)
OWNER_IDS = [owner_id for owner_id, _ in OWNERS]


@pytest.mark.parametrize("owner", [check for _, check in OWNERS], ids=OWNER_IDS)
@pytest.mark.parametrize("sample", PUBLIC_SAFE_SAMPLES, ids=_ids(PUBLIC_SAFE_SAMPLES))
def test_public_safe_corpus_is_accepted_by_every_owner(
    owner: Any,
    sample: tuple[str, str],
) -> None:
    owner(sample[1])


@pytest.mark.parametrize("owner", [check for _, check in OWNERS], ids=OWNER_IDS)
@pytest.mark.parametrize(
    "sample", PRIVATE_LOOKING_SAMPLES, ids=_ids(PRIVATE_LOOKING_SAMPLES)
)
def test_private_looking_corpus_is_rejected_by_every_owner(
    owner: Any,
    sample: tuple[str, str],
) -> None:
    with pytest.raises(ValueError, match="private-looking value"):
        owner(sample[1])


def test_corpus_covers_the_reviewed_credential_shapes() -> None:
    """Guard the corpus itself: the three shapes that motivated this contract."""

    required = {
        "raw_header_basic",
        "assignment_bearer",
        "quoted_json_key_basic",
    }
    assert required <= set(_ids(PRIVATE_LOOKING_SAMPLES))
    assert "governance_prose_needs_owner_authorization" in _ids(PUBLIC_SAFE_SAMPLES)
