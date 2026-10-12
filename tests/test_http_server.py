"""Tests for the HTTP API server."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from loopx.agents.bus import MessageBus
from loopx.agents.channels.web import WebChannelAdapter
from loopx.agents.http_server import (
    AgentHTTPServer,
    _cors_preflight,
    _json_response,
)
from loopx.agents.supervisor import AgentSupervisor
from loopx.agents.tiers.task import TaskRouter


class TestJsonResponse:
    def test_formats_json_200(self) -> None:
        raw = _json_response("200 OK", {"status": "running"})
        header, _, body = raw.partition(b"\r\n\r\n")
        assert b"200 OK" in header
        assert b"application/json" in header
        parsed = json.loads(body)
        assert parsed["status"] == "running"

    def test_formats_json_400(self) -> None:
        raw = _json_response("400 Bad Request", {"error": "bad"})
        assert b"400 Bad Request" in raw

    def test_cors_headers_present(self) -> None:
        raw = _json_response("200 OK", {})
        assert b"Access-Control-Allow-Origin: *" in raw


class TestCorsPreflight:
    def test_returns_204(self) -> None:
        raw = _cors_preflight()
        assert b"204 No Content" in raw
        assert b"Access-Control-Allow-Methods" in raw


class TestWebChannelAdapterAsync:
    def test_healthcheck(self) -> None:
        adapter = WebChannelAdapter()
        health = adapter.healthcheck()
        assert health["channel"] == "web"
        assert health["active_sse_streams"] == 0

    def test_handle_user_message(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                adapter = WebChannelAdapter()
                await adapter.start(bus)

                result = await adapter.handle_user_message(text="hello")
                assert "correlation_id" in result
                assert "events_url" in result
                assert result["events_url"].startswith("/api/chat/events")

                assert result["correlation_id"] in adapter._sse_queues

                await adapter.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_deliver_to_sse_queue(self) -> None:
        async def _run() -> None:
            from loopx.agents.message import (
                MANAGER_ADDRESS,
                MessageEnvelope,
                MessageType,
                channel_address,
            )

            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                adapter = WebChannelAdapter()
                await adapter.start(bus)

                result = await adapter.handle_user_message(text="test")
                cid = result["correlation_id"]

                env = MessageEnvelope.create(
                    source=MANAGER_ADDRESS,
                    target=channel_address("web", "channel-web"),
                    message_type=MessageType.AGENT_REPLY,
                    payload={"text": "response"},
                    correlation_id=cid,
                )
                receipt = await adapter.deliver(env)
                assert receipt.success

                q = adapter._sse_queues[cid]
                event = q.get_nowait()
                assert event["kind"] == "answer.final"

                await adapter.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_deliver_no_queue_fails(self) -> None:
        async def _run() -> None:
            from loopx.agents.message import (
                MANAGER_ADDRESS,
                MessageEnvelope,
                MessageType,
                channel_address,
            )

            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                adapter = WebChannelAdapter()
                await adapter.start(bus)

                env = MessageEnvelope.create(
                    source=MANAGER_ADDRESS,
                    target=channel_address("web", "channel-web"),
                    message_type=MessageType.AGENT_REPLY,
                    payload={"text": "lost"},
                    correlation_id="nonexistent",
                )
                receipt = await adapter.deliver(env)
                assert not receipt.success

                await adapter.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_close_sse_stream(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                adapter = WebChannelAdapter()
                await adapter.start(bus)

                result = await adapter.handle_user_message(text="test")
                cid = result["correlation_id"]
                assert cid in adapter._sse_queues

                adapter.close_sse_stream(cid)
                assert cid not in adapter._sse_queues

                await adapter.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_sse_event_mapping_status_delta(self) -> None:
        from loopx.agents.message import MessageEnvelope, MessageType, MANAGER_ADDRESS, channel_address
        env = MessageEnvelope.create(
            source=MANAGER_ADDRESS,
            target=channel_address("web", "channel-web"),
            message_type=MessageType.STATUS_UPDATE,
            payload={"text": "thinking...", "delta": True},
        )
        event = WebChannelAdapter._envelope_to_sse_event(env)
        assert event["kind"] == "answer.delta"

    def test_sse_event_mapping_status_phase(self) -> None:
        from loopx.agents.message import MessageEnvelope, MessageType, MANAGER_ADDRESS, channel_address
        env = MessageEnvelope.create(
            source=MANAGER_ADDRESS,
            target=channel_address("web", "channel-web"),
            message_type=MessageType.STATUS_UPDATE,
            payload={"text": "planning..."},
        )
        event = WebChannelAdapter._envelope_to_sse_event(env)
        assert event["kind"] == "agent.phase"


class TestAgentHTTPServer:
    def test_server_start_stop(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                supervisor = AgentSupervisor(bus=bus, runtime_root=Path(tmp))
                adapter = WebChannelAdapter()
                router = TaskRouter(runtime_root=Path(tmp))

                await adapter.start(bus)
                await router.start(bus)

                server = AgentHTTPServer(
                    host="127.0.0.1",
                    port=0,  # OS picks a free port
                    web_adapter=adapter,
                    task_router=router,
                    supervisor=supervisor,
                    static_dir=Path(tmp),
                )
                await server.start()
                assert server._server is not None
                await server.stop()
                await router.stop()
                await adapter.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_status_api(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                supervisor = AgentSupervisor(bus=bus, runtime_root=Path(tmp))
                adapter = WebChannelAdapter()
                router = TaskRouter(runtime_root=Path(tmp))

                await adapter.start(bus)
                await router.start(bus)

                server = AgentHTTPServer(
                    host="127.0.0.1",
                    port=18900,
                    web_adapter=adapter,
                    task_router=router,
                    supervisor=supervisor,
                    static_dir=Path(tmp),
                )
                await server.start()

                reader, writer = await asyncio.open_connection("127.0.0.1", 18900)
                writer.write(b"GET /api/status HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()

                response = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                writer.close()

                assert b"200 OK" in response
                header, _, body = response.partition(b"\r\n\r\n")
                data = json.loads(body)
                assert data["status"] == "running"
                assert "agents" in data
                assert "tasks" in data

                await server.stop()
                await router.stop()
                await adapter.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_tasks_api(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                supervisor = AgentSupervisor(bus=bus, runtime_root=Path(tmp))
                adapter = WebChannelAdapter()
                router = TaskRouter(runtime_root=Path(tmp))

                await adapter.start(bus)
                await router.start(bus)

                server = AgentHTTPServer(
                    host="127.0.0.1",
                    port=18901,
                    web_adapter=adapter,
                    task_router=router,
                    supervisor=supervisor,
                    static_dir=Path(tmp),
                )
                await server.start()

                reader, writer = await asyncio.open_connection("127.0.0.1", 18901)
                writer.write(b"GET /api/tasks HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()

                response = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                writer.close()

                assert b"200 OK" in response
                header, _, body = response.partition(b"\r\n\r\n")
                data = json.loads(body)
                assert "tasks" in data
                assert isinstance(data["tasks"], list)

                await server.stop()
                await router.stop()
                await adapter.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_chat_send_api(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                await bus.start()
                supervisor = AgentSupervisor(bus=bus, runtime_root=Path(tmp))
                adapter = WebChannelAdapter()
                router = TaskRouter(runtime_root=Path(tmp))

                await adapter.start(bus)
                await router.start(bus)

                server = AgentHTTPServer(
                    host="127.0.0.1",
                    port=18902,
                    web_adapter=adapter,
                    task_router=router,
                    supervisor=supervisor,
                    static_dir=Path(tmp),
                )
                await server.start()

                body = json.dumps({"text": "hello"}).encode()
                reader, writer = await asyncio.open_connection("127.0.0.1", 18902)
                request = (
                    f"POST /api/chat/send HTTP/1.1\r\n"
                    f"Host: localhost\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"\r\n"
                ).encode() + body
                writer.write(request)
                await writer.drain()

                response = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                writer.close()

                assert b"200 OK" in response
                header, _, resp_body = response.partition(b"\r\n\r\n")
                data = json.loads(resp_body)
                assert "correlation_id" in data
                assert "events_url" in data

                await server.stop()
                await router.stop()
                await adapter.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_chat_send_empty_text_returns_400(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                await bus.start()
                supervisor = AgentSupervisor(bus=bus, runtime_root=Path(tmp))
                adapter = WebChannelAdapter()
                router = TaskRouter(runtime_root=Path(tmp))

                await adapter.start(bus)
                await router.start(bus)

                server = AgentHTTPServer(
                    host="127.0.0.1",
                    port=18903,
                    web_adapter=adapter,
                    task_router=router,
                    supervisor=supervisor,
                    static_dir=Path(tmp),
                )
                await server.start()

                body = json.dumps({"text": ""}).encode()
                reader, writer = await asyncio.open_connection("127.0.0.1", 18903)
                request = (
                    f"POST /api/chat/send HTTP/1.1\r\n"
                    f"Host: localhost\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"\r\n"
                ).encode() + body
                writer.write(request)
                await writer.drain()

                response = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                writer.close()

                assert b"400 Bad Request" in response

                await server.stop()
                await router.stop()
                await adapter.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_404_for_unknown_api(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                supervisor = AgentSupervisor(bus=bus, runtime_root=Path(tmp))
                adapter = WebChannelAdapter()
                router = TaskRouter(runtime_root=Path(tmp))

                await adapter.start(bus)
                await router.start(bus)

                server = AgentHTTPServer(
                    host="127.0.0.1",
                    port=18904,
                    web_adapter=adapter,
                    task_router=router,
                    supervisor=supervisor,
                    static_dir=Path(tmp),
                )
                await server.start()

                reader, writer = await asyncio.open_connection("127.0.0.1", 18904)
                writer.write(b"GET /api/nonexistent HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()

                response = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                writer.close()

                assert b"404 Not Found" in response

                await server.stop()
                await router.stop()
                await adapter.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_static_file_serving(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                # Create a static file
                static_dir = Path(tmp) / "static"
                static_dir.mkdir()
                (static_dir / "index.html").write_text("<h1>LoopX</h1>")
                (static_dir / "test.js").write_text("console.log('ok')")

                bus = MessageBus(runtime_root=Path(tmp))
                supervisor = AgentSupervisor(bus=bus, runtime_root=Path(tmp))
                adapter = WebChannelAdapter()
                router = TaskRouter(runtime_root=Path(tmp))

                await adapter.start(bus)
                await router.start(bus)

                server = AgentHTTPServer(
                    host="127.0.0.1",
                    port=18905,
                    web_adapter=adapter,
                    task_router=router,
                    supervisor=supervisor,
                    static_dir=static_dir,
                )
                await server.start()

                # Test index.html
                reader, writer = await asyncio.open_connection("127.0.0.1", 18905)
                writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                response = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                writer.close()
                assert b"200 OK" in response
                assert b"<h1>LoopX</h1>" in response

                # Test SPA fallback
                reader2, writer2 = await asyncio.open_connection("127.0.0.1", 18905)
                writer2.write(b"GET /chat HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer2.drain()
                response2 = await asyncio.wait_for(reader2.read(4096), timeout=5.0)
                writer2.close()
                assert b"200 OK" in response2
                assert b"<h1>LoopX</h1>" in response2

                await server.stop()
                await router.stop()
                await adapter.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_cors_preflight(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                supervisor = AgentSupervisor(bus=bus, runtime_root=Path(tmp))
                adapter = WebChannelAdapter()
                router = TaskRouter(runtime_root=Path(tmp))

                await adapter.start(bus)
                await router.start(bus)

                server = AgentHTTPServer(
                    host="127.0.0.1",
                    port=18906,
                    web_adapter=adapter,
                    task_router=router,
                    supervisor=supervisor,
                    static_dir=Path(tmp),
                )
                await server.start()

                reader, writer = await asyncio.open_connection("127.0.0.1", 18906)
                writer.write(b"OPTIONS /api/chat/send HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()

                response = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                writer.close()

                assert b"204 No Content" in response
                assert b"Access-Control-Allow-Methods" in response

                await server.stop()
                await router.stop()
                await adapter.stop()
                await bus.shutdown()

        asyncio.run(_run())
