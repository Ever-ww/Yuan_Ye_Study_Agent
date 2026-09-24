from __future__ import annotations

import asyncio
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from Agent import AgentRuntime, load_runtime_config
from Agent.models.providers import OpenAICompatibleProvider
from gateway.application import GatewayApplication
from gateway.models import RunCreateRequest
from memory import MemoryStore


class _LocalOpenAIHandler(BaseHTTPRequestHandler):
    """A deterministic provider boundary; Gateway and Runtime remain real."""

    requests: list[dict[str, Any]] = []
    lock = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler API
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length))
        with self.lock:
            self.requests.append(payload)
            has_tool_result = any(
                item.get("role") == "tool"
                for item in payload.get("messages", [])
                if isinstance(item, dict)
            )
            if has_tool_result:
                message = {
                    "role": "assistant",
                    "content": "4",
                    "tool_calls": [],
                }
            else:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call-local-calculator",
                        "type": "function",
                        "function": {
                            "name": "calculator",
                            "arguments": json.dumps(
                                {"expression": "2 + 2"},
                                separators=(",", ":"),
                            ),
                        },
                    }],
                }
        if payload.get("stream"):
            if message["tool_calls"]:
                packets = [
                    {"choices": [{"delta": {"tool_calls": [{
                        "index": 0,
                        "id": "call-local-calculator",
                        "function": {"name": "calculator", "arguments": '{"expression":"'},
                    }]}}]},
                    {"choices": [{"delta": {"tool_calls": [{
                        "index": 0,
                        "function": {"arguments": "2 + 2\"}"},
                    }]}}]},
                ]
            else:
                packets = [
                    {"choices": [{"delta": {"content": ""}}]},
                    {"choices": [{"delta": {"content": "4"}}]},
                ]
            packets.append({
                "choices": [],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            })
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for packet in packets:
                self.wfile.write(f"data: {json.dumps(packet)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        body = json.dumps({
            "id": "local-response",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if message["tool_calls"] else "stop",
            }],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


def _start_provider_server() -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    _LocalOpenAIHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalOpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}/v1"


@pytest.mark.integration
@pytest.mark.parametrize("streaming", [False, True], ids=["complete", "stream"])
def test_gateway_runtime_tool_event_and_session_persistence_use_real_local_chain(
    tmp_path: Path,
    streaming: bool,
) -> None:
    async def exercise() -> None:
        server, thread, base_url = _start_provider_server()
        agent_root = tmp_path / "agent-home"
        workspace = tmp_path / "workspace"
        agent_root.mkdir()
        workspace.mkdir()

        def runtime_factory(workspace_root: Path, approval) -> AgentRuntime:
            config = load_runtime_config(agent_root, workspace_root=workspace_root)
            memory = MemoryStore(
                config.memory_dir,
                workspace_root=workspace_root,
                agent_root=agent_root,
                partition_by_workspace=False,
            )
            provider = OpenAICompatibleProvider(
                base_url,
                "local-test-model",
                "local-test-key",
                streaming=streaming,
                reasoning_effort="none",
            )
            return AgentRuntime(
                config,
                provider=provider,
                memory=memory,
                approval=approval,
                enable_sandbox=False,
                enable_context_processing=False,
                enable_skills=False,
                enable_subagent=False,
                enable_extensions=False,
                enable_references=False,
                enable_cron=False,
            )

        source_root = tmp_path / "agent-source"
        source_root.mkdir()
        (source_root / "harness-evolution").mkdir()
        shutil.copy2(
            Path(__file__).resolve().parents[1] / "harness-evolution" / "harness.py",
            source_root / "harness-evolution" / "harness.py",
        )
        app_config = load_runtime_config(agent_root, workspace_root=workspace).model_copy(
            update={"coding_source_root": source_root},
        )
        application = GatewayApplication(
            app_config,
            runtime_factory=runtime_factory,
        )
        restarted: GatewayApplication | None = None
        try:
            project = application.register_project(workspace, "Local integration")
            run = await application.start_run(RunCreateRequest(
                project_id=project.project_id,
                client_id="pytest-local-integration",
                task="calculate 2 + 2",
                reasoning_effort="none",
            ))
            deadline = asyncio.get_running_loop().time() + 10
            while application.store.run(run.run_id).status not in {
                "completed", "failed", "cancelled", "interrupted",
            }:
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("local Gateway run did not reach a terminal state")
                await asyncio.sleep(0.01)

            final = application.store.run(run.run_id)
            assert final.status == "completed"
            assert final.session_id
            records = application.memory_for_project(project.project_id).session_records(
                final.session_id,
            )
            assert [record["role"] for record in records] == [
                "user", "assistant", "tool", "assistant",
            ]
            assert records[2]["name"] == "calculator"
            assert records[2]["status"] == "success"
            assert records[-1]["content"] == "4"

            events = application.event_store.read_stream(run.run_id)
            event_types = [
                (
                    event.envelope.type.value
                    if hasattr(event.envelope.type, "value")
                    else str(event.envelope.type)
                )
                for event in events
            ]
            assert "tool_requested" in event_types
            assert "tool_completed" in event_types
            assert "run_completed" in event_types
            assert len(_LocalOpenAIHandler.requests) == 2
            assert any(
                item.get("role") == "tool"
                for item in _LocalOpenAIHandler.requests[-1]["messages"]
            )

            # Reconstruct the real Gateway against the same durable stores.
            # No fake runtime or in-memory projection is used for recovery.
            await application.close()
            restarted = GatewayApplication(app_config, runtime_factory=runtime_factory)
            restored = restarted.store.run(run.run_id)
            assert restored.status == "completed"
            assert restored.session_id == final.session_id
            restored_records = restarted.memory_for_project(
                project.project_id,
            ).session_records(restored.session_id)
            assert restored_records[-1]["content"] == "4"
            restored_types = [
                event.envelope.type.value
                if hasattr(event.envelope.type, "value")
                else str(event.envelope.type)
                for event in restarted.event_store.read_stream(run.run_id)
            ]
            assert "run_completed" in restored_types
        finally:
            if restarted is not None:
                await restarted.close()
            else:
                await application.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    asyncio.run(exercise())
