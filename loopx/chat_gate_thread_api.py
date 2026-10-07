"""Loopback Chat API for user gate discussion threads (fork slice S6).

``GET  /api/chat/gate-thread?goal_id=G&todo_id=T`` returns the thread and gate
status (``loopx gate show``). ``POST /api/chat/gate-thread/reply`` with
``{goal_id, todo_id, text}`` appends an owner (user) reply
(``loopx gate reply``), which marks the gate ``awaiting_orchestrator`` so the
dispatcher wakes the orchestrator. The dashboard is owner-local and
loopback-only, so a reply here is the owner's; the orchestrator replies through
the CLI with its agent id. Closing the gate stays on ``gate.resolve``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

from .gate_threads import AUTHOR_USER, GateThreadError, gate_view, reply_to_gate
from .status_server import is_loopback_host

CHAT_GATE_THREAD_PATH = "/api/chat/gate-thread"
CHAT_GATE_THREAD_REPLY_PATH = "/api/chat/gate-thread/reply"


class GateThreadRequestMixin:
    server: Any
    path: str

    def _read_json(self) -> dict[str, Any]:
        raise NotImplementedError

    def _send_error(self, message: str, **kwargs: Any) -> None:
        raise NotImplementedError

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        raise NotImplementedError

    def _gate_thread_runtime_root(self) -> Any:
        from .paths import effective_runtime_root

        return effective_runtime_root(self.server.registry_path, self.server.runtime_root_override)

    def _gate_thread_loopback(self) -> bool:
        if is_loopback_host(str(self.server.server_address[0])):
            return True
        self._send_error("Gate threads require a loopback LoopX Chat server.", status=403)
        return False

    def _require_loopback_origin(self) -> bool:
        raise NotImplementedError

    def _gate_thread(self) -> None:
        if not self._gate_thread_loopback() or not self._require_loopback_origin():
            return
        query = parse_qs(urlparse(self.path).query)
        goal_id = (query.get("goal_id") or [""])[0].strip()
        todo_id = (query.get("todo_id") or [""])[0].strip()
        if not goal_id or not todo_id:
            self._send_error("goal_id and todo_id are required", status=400)
            return
        try:
            payload = gate_view(
                registry_path=self.server.registry_path, runtime_root=self._gate_thread_runtime_root(),
                goal_id=goal_id, todo_id=todo_id, runtime_root_arg=self.server.runtime_root_override,
            )
        except GateThreadError as error:
            self._send_error(str(error), status=404 if error.code in {"gate_not_found", "goal_not_registered"} else 400,
                             error_code=error.code)
            return
        self._send_json(payload)

    def _gate_thread_reply(self) -> None:
        if not self._gate_thread_loopback():
            return
        try:
            body = self._read_json()
            unknown = sorted(set(body) - {"goal_id", "todo_id", "text"})
            if unknown:
                raise GateThreadError("unknown_fields", "unknown gate reply field(s): " + ", ".join(unknown))
            goal_id = str(body.get("goal_id") or "").strip()
            todo_id = str(body.get("todo_id") or "").strip()
            if not goal_id or not todo_id:
                raise GateThreadError("missing_fields", "goal_id and todo_id are required")
            payload = reply_to_gate(
                registry_path=self.server.registry_path, runtime_root=self._gate_thread_runtime_root(),
                goal_id=goal_id, todo_id=todo_id, text=str(body.get("text") or ""), author=AUTHOR_USER,
                runtime_root_arg=self.server.runtime_root_override,
            )
        except GateThreadError as error:
            status = 404 if error.code in {"gate_not_found", "goal_not_registered"} else 409 if error.code == "gate_closed" else 400
            self._send_error(str(error), status=status, error_code=error.code)
            return
        except ValueError as error:
            self._send_error(str(error), status=400)
            return
        self._send_json(payload)
