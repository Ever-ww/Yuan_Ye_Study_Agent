from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from Agent import load_runtime_config
from dream import DreamScheduler, DreamService, DreamStatus, SessionArchiveReader
from gateway.api import create_gateway_api
from gateway.application import GatewayApplication
from memory import MemoryStore
from run_ui.cli import _handle_dream_command


def _append(
    memory: MemoryStore,
    session_id: str,
    role: str,
    content: str,
    timestamp: str,
    **metadata,
) -> None:
    memory.sessions.append(
        session_id,
        role,
        content,
        {"timestamp": timestamp, **metadata},
    )


class _DreamModel:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, messages):
        self.calls += 1
        payload = json.loads(messages[-1]["content"])
        if "conversation_records" in payload:
            user = next(item for item in payload["conversation_records"] if item["role"] == "user")
            return json.dumps({"candidates": [{
                "target_file": "USER.md",
                "statement": "用户偏好中文技术说明",
                "operation": "insert",
                "memory_id": None,
                "evidence_ids": [user["evidence_id"]],
                "confidence": 0.95,
                "reason": "用户明确表达",
            }]}, ensure_ascii=False)
        return json.dumps({"candidates": payload["extracted_candidates"]}, ensure_ascii=False)


class DreamTests(unittest.TestCase):
    def test_archive_scans_all_workspaces_and_only_user_owns_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root, dream_enabled=False)
            first = MemoryStore(config.memory_dir, workspace_root=root / "one", agent_root=root)
            second = MemoryStore(config.memory_dir, workspace_root=root / "two", agent_root=root)
            selected = date(2026, 8, 3)
            for memory, session_id in ((first, "a" * 16), (second, "b" * 16)):
                memory.create_session("first", session_id)
                _append(memory, session_id, "user", "我偏好中文", "2026-08-03 10:00:00", origin="interactive")
                _append(memory, session_id, "assistant", "已经记录", "2026-08-03 10:00:01", reasoning="不应进入")
            _append(first, "a" * 16, "tool", "秘密工具输出", "2026-08-03 10:00:02", tool_call_id="call", name="read")
            cron = "c" * 16
            first.create_session("cron", cron)
            _append(first, cron, "user", "自动任务", "2026-08-03 11:00:00", origin="cron")

            archive = SessionArchiveReader(config.memory_dir / "session").iter_day(
                selected, "Asia/Shanghai",
            )
            self.assertEqual(len(archive.evidence), 2)
            self.assertEqual({item.role for item in archive.records}, {"user", "assistant"})
            self.assertTrue(all(item.evidence_id for item in archive.records if item.role == "user"))
            self.assertTrue(all(item.evidence_id is None for item in archive.records if item.role == "assistant"))
            self.assertNotIn("秘密工具输出", str(archive.model_dump()))
            self.assertNotIn("自动任务", str(archive.model_dump()))

    def test_process_day_is_idempotent_preserves_manual_profile_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root, dream_enabled=False, dream_timezone="Asia/Shanghai")
            memory = MemoryStore(config.memory_dir, workspace_root=root / "workspace", agent_root=root)
            session_id = "d" * 16
            memory.create_session("first", session_id)
            _append(memory, session_id, "user", "以后请用中文解释技术问题", "2026-08-03 09:00:00")
            profile = config.memory_dir / "profile" / "USER.md"
            profile.write_text("# 用户手写\n不要覆盖\n", encoding="utf-8")
            model = _DreamModel()
            service = DreamService(config, model_runner=model)

            result = asyncio.run(service.process_day(date(2026, 8, 3)))
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.memories_changed, 1)
            self.assertGreater(result.input_tokens, 0)
            self.assertGreater(result.output_tokens, 0)
            content = profile.read_text(encoding="utf-8")
            self.assertIn("# 用户手写\n不要覆盖", content)
            self.assertIn("用户偏好中文技术说明", content)
            calls = model.calls

            repeated = asyncio.run(service.process_day(date(2026, 8, 3)))
            self.assertEqual(repeated.status, "noop")
            self.assertEqual(model.calls, calls)

            rollback = asyncio.run(service.rollback(result.run_id))
            self.assertTrue(rollback.restored)
            self.assertEqual(profile.read_text(encoding="utf-8"), "# 用户手写\n不要覆盖\n")

    def test_invalid_model_output_never_changes_profile(self) -> None:
        async def invalid(messages):
            del messages
            return "not json"

        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root, dream_enabled=False)
            memory = MemoryStore(config.memory_dir, workspace_root=root / "workspace", agent_root=root)
            session_id = "e" * 16
            memory.create_session("first", session_id)
            _append(memory, session_id, "user", "记住我喜欢简洁回答", "2026-08-03 09:00:00")
            profile = config.memory_dir / "profile" / "USER.md"
            before = profile.read_bytes()
            result = asyncio.run(DreamService(config, model_runner=invalid).process_day(date(2026, 8, 3)))
            self.assertEqual(result.status, "failed")
            self.assertEqual(profile.read_bytes(), before)

    def test_profile_update_preserves_text_around_managed_block(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root, dream_enabled=False)
            memory = MemoryStore(config.memory_dir, workspace_root=root / "workspace", agent_root=root)
            session_id = "9" * 16
            memory.create_session("first", session_id)
            _append(memory, session_id, "user", "以后请使用中文", "2026-08-03 09:00:00")
            profile = config.memory_dir / "profile" / "USER.md"
            prefix = "# 用户手写头部\n\n"
            suffix = "\n\n## 用户手写尾部\n保留原样\n"
            profile.write_text(
                prefix
                + "<!-- dream:managed:start -->\n旧内容\n<!-- dream:managed:end -->"
                + suffix,
                encoding="utf-8",
            )

            result = asyncio.run(DreamService(config, model_runner=_DreamModel()).process_day(date(2026, 8, 3)))

            self.assertEqual(result.status, "completed")
            updated = profile.read_text(encoding="utf-8")
            self.assertTrue(updated.startswith(prefix))
            self.assertTrue(updated.endswith(suffix))
            self.assertIn("用户偏好中文技术说明", updated)

    def test_profile_transaction_failure_restores_every_modified_file(self) -> None:
        class TwoProfileModel:
            async def __call__(self, messages):
                payload = json.loads(messages[-1]["content"])
                if "conversation_records" in payload:
                    evidence = next(
                        item["evidence_id"] for item in payload["conversation_records"]
                        if item["role"] == "user"
                    )
                    candidates = [
                        {
                            "target_file": target,
                            "statement": statement,
                            "operation": "insert",
                            "memory_id": None,
                            "evidence_ids": [evidence],
                            "confidence": 0.9,
                            "reason": "用户明确表达",
                        }
                        for target, statement in (
                            ("USER.md", "用户偏好中文"),
                            ("RESEARCH.md", "用户研究上下文工程"),
                        )
                    ]
                    return json.dumps({"candidates": candidates}, ensure_ascii=False)
                return json.dumps({"candidates": payload["extracted_candidates"]}, ensure_ascii=False)

        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root, dream_enabled=False)
            memory = MemoryStore(config.memory_dir, workspace_root=root / "workspace", agent_root=root)
            session_id = "8" * 16
            memory.create_session("first", session_id)
            _append(memory, session_id, "user", "我偏好中文并研究上下文工程", "2026-08-03 09:00:00")
            user = config.memory_dir / "profile" / "USER.md"
            research = config.memory_dir / "profile" / "RESEARCH.md"
            before_user, before_research = user.read_bytes(), research.read_bytes()

            from dream import service as dream_service

            original_write = dream_service._write_text_atomic
            failed = False

            def fail_second_profile(path, content):
                nonlocal failed
                if path.name == "RESEARCH.md" and not failed:
                    failed = True
                    raise OSError("simulated profile failure")
                original_write(path, content)

            with patch.object(dream_service, "_write_text_atomic", side_effect=fail_second_profile):
                result = asyncio.run(
                    DreamService(config, model_runner=TwoProfileModel()).process_day(date(2026, 8, 3)),
                )

            self.assertEqual(result.status, "failed")
            self.assertEqual(user.read_bytes(), before_user)
            self.assertEqual(research.read_bytes(), before_research)
            memories = json.loads((root / ".yy" / "dream" / "memories.json").read_text(encoding="utf-8"))
            self.assertEqual(memories["memories"], {})

    def test_scheduler_first_run_waits_until_local_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(
                root, dream_enabled=True, dream_timezone="Asia/Shanghai", dream_schedule="0 3 * * *",
            )
            service = DreamService(config, model_runner=_DreamModel())

            async def callback(result, automatic):
                del result, automatic

            before = DreamScheduler(
                service, lambda: True, callback,
                clock=lambda: datetime(2026, 8, 4, 2, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
            self.assertIsNone(before._due_date(before._local_now()))
            after = DreamScheduler(
                service, lambda: True, callback,
                clock=lambda: datetime(2026, 8, 4, 4, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
            self.assertEqual(after._due_date(after._local_now()), date(2026, 8, 3))

    def test_gateway_dream_api(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root, dream_enabled=False, dream_timezone="Asia/Shanghai")
            memory = MemoryStore(config.memory_dir, workspace_root=root / "workspace", agent_root=root)
            session_id = "f" * 16
            memory.create_session("first", session_id)
            _append(memory, session_id, "user", "我偏好中文", "2026-08-03 10:00:00")
            application = GatewayApplication(config)
            application.dream_service.model_runner = _DreamModel()
            headers = {"Authorization": "Bearer test-token"}
            with TestClient(create_gateway_api(application, access_token="test-token")) as client:
                status = client.get("/api/v1/dream/status", headers=headers)
                self.assertEqual(status.status_code, 200)
                response = client.post(
                    "/api/v1/dream/run", headers=headers, json={"date": "2026-08-03"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "completed")

    def test_cli_bare_dream_command_displays_status(self) -> None:
        class Client:
            called = False

            async def dream_status(self):
                self.called = True
                return DreamStatus(
                    enabled=True,
                    running=False,
                    schedule="0 3 * * *",
                    timezone="Asia/Shanghai",
                    initialized_at="2026-08-04T00:00:00+08:00",
                )

        client = Client()
        asyncio.run(_handle_dream_command(client, "/dream"))
        self.assertTrue(client.called)


if __name__ == "__main__":
    unittest.main()
