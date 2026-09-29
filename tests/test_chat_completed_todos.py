import json
import threading
from urllib.request import urlopen
from urllib.request import Request
from urllib.error import HTTPError

import pytest

from loopx.chat_completed_todos import CompletedTodoPages, _verify_goal_result_page
from loopx.chat_server import ChatHTTPServer, ChatRequestHandler
from loopx.control_plane.todos.contract import encode_metadata_value


def test_snapshot_pagination_is_bounded_and_stable():
    pages = CompletedTodoPages()
    rows = [{"todo_id": f"todo_{index}"} for index in range(4087)]
    calls = []
    def load():
        calls.append(True)
        return list(rows)
    result = pages.page(scope=("goal", "all"), cursor="", load=load)
    found = list(result["items"])
    rows.insert(0, {"todo_id": "todo_new"})
    while result["next_cursor"]:
        assert len(result["items"]) <= 40
        result = pages.page(scope=("goal", "all"), cursor=result["next_cursor"], load=load)
        found.extend(result["items"])
    assert len(found) == len({row["todo_id"] for row in found}) == 4087
    assert len(calls) == 1


def test_cursor_scope_expiry_and_capacity():
    pages = CompletedTodoPages()

    def load():
        return [{}] * 41

    cursor = pages.page(scope="a", cursor="", load=load)["next_cursor"]
    with pytest.raises(ValueError, match="expired"):
        pages.page(scope="b", cursor=cursor, load=load)
    for index in range(9):
        pages.page(scope=index, cursor="", load=load)
    assert len(pages._snapshots) == 8
    with pytest.raises(ValueError, match="expired"):
        pages.page(scope="a", cursor=cursor, load=load)
    cursor = pages.page(scope="a", cursor="", load=load)["next_cursor"]
    pages.ttl_seconds = 0
    with pytest.raises(ValueError, match="expired"):
        pages.page(scope="a", cursor=cursor, load=load)


def test_snapshot_byte_budget_is_enforced():
    pages = CompletedTodoPages()
    pages.max_cache_bytes = 10
    with pytest.raises(ValueError, match="too_large"):
        pages.page(scope="a", cursor="", load=lambda: [{"text": "x" * 100}])
    assert not pages._snapshots


def test_goal_report_verification_is_bounded_by_requested_page(monkeypatch):
    calls = []
    def read(**kwargs):
        calls.append(kwargs["todo_id"])
        return {"result": {"sha256": "a" * 64, "producer_agent_id": "lead",
                           "content_type": "text/markdown", "size_bytes": 12}}
    monkeypatch.setattr("loopx.control_plane.todos.completion_result.read_completion_result", read)
    pages = CompletedTodoPages()
    rows = [{"todo_id": f"todo_report_{index}", "title": "Report",
             "sha256": "a" * 64, "producer_agent_id": "lead", "completed_at": None}
            for index in range(85)]
    first = pages.page(scope=("accepted_goal_results", "goal"), cursor="", load=lambda: rows)
    verified = _verify_goal_result_page(page=first, registry_path=None, runtime_root=None, goal_id="goal")
    assert len(verified["items"]) == len(calls) == 40
    assert verified["next_cursor"]
    assert verified["unavailable_count"] == 0
    assert verified["unavailable_todo_ids"] == []


def test_goal_report_page_names_unavailable_rows_instead_of_hiding_the_rest(monkeypatch):
    def read(**kwargs):
        if kwargs["todo_id"] == "todo_report_stale":
            raise ValueError("completion result acceptance basis is stale")
        return {"result": {"sha256": "a" * 64, "producer_agent_id": "lead",
                           "content_type": "text/markdown", "size_bytes": 12}}
    monkeypatch.setattr("loopx.control_plane.todos.completion_result.read_completion_result", read)
    pages = CompletedTodoPages()
    rows = [{"todo_id": todo_id, "title": "Report", "sha256": "a" * 64,
             "producer_agent_id": "lead", "completed_at": None}
            for todo_id in ("todo_report_stale", "todo_report_planned")]
    page = pages.page(scope=("accepted_goal_results", "goal"), cursor="", load=lambda: rows)
    verified = _verify_goal_result_page(page=page, registry_path=None, runtime_root=None, goal_id="goal")
    # An unrelated unreadable report is named, not turned into a whole-page failure,
    # so a reader that only needs its own Todo ids can still resolve them.
    assert verified["unavailable_count"] == 1
    assert verified["unavailable_todo_ids"] == ["todo_report_stale"]
    assert [row["todo_id"] for row in verified["items"]] == ["todo_report_planned"]


@pytest.mark.parametrize("count", [85, 4087])
def test_http_history_reads_real_markdown_without_writes(tmp_path, count):
    state = tmp_path / "active.md"
    text = (f"Inspect {tmp_path}/results.txt " + "complete task description " * 25)[:420]
    evidence = f"Verified output at {tmp_path}/results.txt"
    state.write_text("# Synthetic Goal\n\n## Agent Todo\n" + "\n".join(
        f"- [x] {text}{index}\n  <!-- loopx:todo todo_id=todo_history_{index} status=done task_class=advancement_task note={encode_metadata_value(evidence)} -->" for index in range(count)
    ) + "\n\n## User Todo\n", encoding="utf-8")
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"common_runtime_root": str(tmp_path / "runtime"), "goals": [{"id": "history-goal", "repo": str(tmp_path), "state_file": "active.md"}]}))
    before = state.read_bytes()
    server = ChatHTTPServer(("127.0.0.1", 0), ChatRequestHandler)
    server.registry_path = registry
    server.runtime_root_override = None
    server.verbose = False
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/chat/completed-todos?goal_id=history-goal"
        with urlopen(url) as response:
            page = json.load(response)
        assert page["total"] == count
        assert len(page["items"]) == 40
        assert page["next_cursor"]
        assert page["items"][0]["text"].startswith(text)
        assert len(page["items"][0]["text"]) > 400
        assert page["items"][0]["evidence"] == evidence
        with pytest.raises(HTTPError) as denied:
            urlopen(Request(url, headers={"Origin": "https://unrelated.example"}))
        assert denied.value.code == 403
        assert state.read_bytes() == before
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
