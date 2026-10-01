from pathlib import Path

from loopx.control_plane.quota import slot_accounting


ROOT = Path(__file__).resolve().parents[2]


def test_settlement_inference_bug_note_references_current_helper() -> None:
    note = (
        ROOT / "docs/development/bugs/settlement-infer-persisted-break.md"
    ).read_text(encoding="utf-8")

    assert "`slot_accounting._latest_unspent_turn_settlement_run`" in note
    assert "_latest_unspent_accountable_delivery_run" not in note
    assert callable(slot_accounting._latest_unspent_turn_settlement_run)
