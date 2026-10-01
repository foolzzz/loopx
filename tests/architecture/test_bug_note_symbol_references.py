from pathlib import Path

from loopx.control_plane.quota import slot_accounting


ROOT = Path(__file__).resolve().parents[2]
RETIRED_SETTLEMENT_HELPER = "_latest_unspent_accountable_delivery_run"
CURRENT_SETTLEMENT_HELPER = "_latest_unspent_turn_settlement_run"
SETTLEMENT_DOCS = (
    "docs/development/bugs/settlement-infer-persisted-break.md",
    "docs/development/control-plane-course/08-evidence-refresh-and-self-repair.md",
)


def test_maintained_docs_do_not_reference_retired_settlement_helper() -> None:
    docs_root = ROOT / "docs"
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in docs_root.rglob("*.md")
        if "archive" not in path.relative_to(docs_root).parts
        and RETIRED_SETTLEMENT_HELPER in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_settlement_docs_reference_current_helper() -> None:
    for relative_path in SETTLEMENT_DOCS:
        content = (ROOT / relative_path).read_text(encoding="utf-8")

        assert CURRENT_SETTLEMENT_HELPER in content
        assert RETIRED_SETTLEMENT_HELPER not in content
    assert callable(slot_accounting._latest_unspent_turn_settlement_run)
