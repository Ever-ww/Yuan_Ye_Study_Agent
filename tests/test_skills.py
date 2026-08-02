"""Skill 获取、审核、可信索引和渐进读取测试。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prompt import PromptComposer
from run_ui.cli import _chat
from skill import SkillInstallRequest, SkillService, parse_skill
from tool import ToolContext, default_tools


def _make_skill(
    root: Path,
    name: str,
    *,
    description: str = "用于测试渐进式能力加载",
    license_value: str | None = "MIT",
    body: str = "这是只有 skill_read 才应读取的正文。",
) -> Path:
    skill = root / name
    skill.mkdir(parents=True, exist_ok=True)
    license_line = f"license: {license_value}\n" if license_value else ""
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{license_line}---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill


class SkillTests(unittest.TestCase):
    def test_parse_and_prompt_only_expose_xml_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            source = _make_skill(root / "sources", "xml-skill", description="处理 A & B")
            metadata = parse_skill(source)
            self.assertEqual(metadata.name, "xml-skill")

            service = SkillService(root, root)
            result = asyncio.run(service.install(SkillInstallRequest(source=str(source))))
            self.assertEqual(result.status, "installed")
            system = PromptComposer(root, service).compose("任务")[0]["content"]
            self.assertIn("<available_skills>", system)
            self.assertIn("处理 A &amp; B", system)
            self.assertIn("skills/xml-skill/SKILL.md", system)
            self.assertNotIn("这是只有 skill_read", system)

    def test_clean_local_skill_installs_and_staging_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            source = _make_skill(root / "sources", "clean-skill")
            service = SkillService(root, root)
            result = asyncio.run(service.install(SkillInstallRequest(source=str(source))))
            self.assertEqual(result.status, "installed")
            self.assertTrue((root / "skills" / "clean-skill" / "SKILL.md").is_file())
            self.assertEqual(service.catalog()[0].name, "clean-skill")
            self.assertFalse(any(service.review_root.iterdir()))
            self.assertTrue((service.audit_root / f"{result.review_id}.json").is_file())

    def test_local_source_must_stay_inside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as agent, tempfile.TemporaryDirectory() as workspace:
            agent_root = Path(agent)
            workspace_root = Path(workspace)
            outside = _make_skill(agent_root / "outside", "outside-skill")
            service = SkillService(agent_root, workspace_root)
            result = asyncio.run(service.install(SkillInstallRequest(source=str(outside))))
            self.assertEqual(result.status, "error")
            self.assertIn("workspace", result.message)
            unsupported = asyncio.run(service.install(SkillInstallRequest(source="https://example.com/x")))
            self.assertEqual(unsupported.status, "error")

    def test_missing_license_and_scripts_require_review(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            source = _make_skill(root / "sources", "review-skill", license_value=None)
            scripts = source / "scripts"
            scripts.mkdir()
            marker = root / "executed.txt"
            (scripts / "run.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n",
                encoding="utf-8",
            )
            approvals: list[tuple[str, dict[str, object]]] = []

            async def decline(name: str, arguments: dict[str, object]) -> bool:
                approvals.append((name, arguments))
                return False

            service = SkillService(root, root, approval=decline)
            result = asyncio.run(service.install(SkillInstallRequest(source=str(source))))
            self.assertEqual(result.status, "declined")
            self.assertEqual(approvals[0][0], "skill_review")
            self.assertFalse(marker.exists())
            report = service.audit_report(result.review_id or "")
            self.assertEqual(report.status, "declined")
            self.assertIn("script", {item.code for item in report.findings})
            self.assertIn("license-unclear", {item.code for item in report.findings})

    def test_embedded_private_key_is_hard_blocked_without_override(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            source = _make_skill(
                root / "sources",
                "blocked-skill",
                body="-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----",
            )
            approvals = 0

            async def approve(name: str, arguments: dict[str, object]) -> bool:
                nonlocal approvals
                approvals += 1
                return True

            service = SkillService(root, root, approval=approve)
            result = asyncio.run(service.install(SkillInstallRequest(source=str(source))))
            self.assertEqual(result.status, "blocked")
            self.assertEqual(approvals, 0)
            self.assertFalse((root / "skills" / "blocked-skill").exists())

    def test_skill_read_rejects_traversal_binary_and_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            source = _make_skill(root / "sources", "read-skill")
            references = source / "references"
            references.mkdir()
            (references / "guide.md").write_text("引用正文", encoding="utf-8")
            (source / "binary.bin").write_bytes(b"\0\x01")
            service = SkillService(root, root)
            result = asyncio.run(service.install(SkillInstallRequest(source=str(source))))
            self.assertEqual(result.status, "installed")
            self.assertEqual(service.read("read-skill", "references/guide.md"), "引用正文")
            with self.assertRaises(PermissionError):
                service.read("read-skill", "../outside.txt")
            with self.assertRaisesRegex(ValueError, "二进制"):
                service.read("read-skill", "binary.bin")
            (root / "skills" / "read-skill" / "references" / "guide.md").write_text("篡改", encoding="utf-8")
            self.assertEqual(service.catalog(), ())
            with self.assertRaisesRegex(RuntimeError, "重新审核"):
                service.read("read-skill")

    def test_multiple_candidates_require_explicit_skill_path(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            repository = root / "repository"
            _make_skill(repository / "one", "first-skill")
            _make_skill(repository / "two", "second-skill")
            service = SkillService(root, root)
            result = asyncio.run(service.install(SkillInstallRequest(source=str(repository))))
            self.assertEqual(result.status, "selection_required")
            self.assertEqual(result.candidates, ("one/first-skill", "two/second-skill"))
            selected = asyncio.run(service.install(SkillInstallRequest(
                source=str(repository),
                skill_path="one/first-skill",
            )))
            self.assertEqual(selected.status, "installed")

    def test_update_requires_approval_and_keeps_backup(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            first = _make_skill(root / "v1", "update-skill", body="第一版")
            second = _make_skill(root / "v2", "update-skill", body="第二版")
            approvals: list[str] = []

            async def approve(name: str, arguments: dict[str, object]) -> bool:
                approvals.append(name)
                return True

            service = SkillService(root, root, approval=approve)
            self.assertEqual(
                asyncio.run(service.install(SkillInstallRequest(source=str(first)))).status,
                "installed",
            )
            result = asyncio.run(service.update(SkillInstallRequest(
                source=str(second),
                action="update",
                name="update-skill",
            )))
            self.assertEqual(result.status, "installed")
            self.assertEqual(approvals, ["skill_review"])
            self.assertIn("第二版", service.read("update-skill"))
            backups = list((service.backup_root / "update-skill").iterdir())
            self.assertEqual(len(backups), 1)
            self.assertIn("第一版", (backups[0] / "SKILL.md").read_text(encoding="utf-8"))

    def test_tool_install_has_initial_approval_and_subagent_cannot_select_it(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            source = _make_skill(root / "sources", "tool-skill")
            service = SkillService(root, root)
            registry = default_tools(root, skill_service=service)
            approvals: list[str] = []

            async def approve(name: str, arguments: dict[str, object]) -> bool:
                approvals.append(name)
                return True

            output = asyncio.run(registry.execute(
                "skill_install",
                {"source": str(source)},
                ToolContext(project_root=root, approval=approve),
            ))
            self.assertEqual(json.loads(output)["status"], "installed")
            self.assertEqual(approvals, ["skill_install"])
            with self.assertRaisesRegex(ValueError, "不允许"):
                registry.select(["skill_install"])
            selected = registry.select(["skill_read"])
            self.assertEqual(selected.names(), ("skill_read",))

    def test_public_github_clone_records_ref_commit_and_disables_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            calls: list[list[str]] = []

            async def command(arguments, *, cwd, env, timeout):
                del cwd, timeout
                calls.append(list(arguments))
                self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
                if "clone" in arguments:
                    checkout = Path(arguments[-1])
                    _make_skill(checkout.parent, checkout.name)
                    return ""
                return "a" * 40 + "\n"

            service = SkillService(root, root)
            with patch("skill.service._run_command", side_effect=command):
                result = asyncio.run(service.install(SkillInstallRequest(
                    source="https://github.com/acme/github-skill",
                    ref="main",
                )))
            self.assertEqual(result.status, "installed")
            self.assertIn("credential.helper=", calls[0])
            self.assertIn("--branch", calls[0])
            index = json.loads(service.index_path.read_text(encoding="utf-8"))
            self.assertEqual(index["skills"]["github-skill"]["source"]["commit"], "a" * 40)

            invalid = asyncio.run(service.install(SkillInstallRequest(
                source="https://github.com/acme/another-skill",
                ref="--upload-pack",
            )))
            self.assertEqual(invalid.status, "error")


    def test_explicit_chat_skill_command_does_not_enter_runtime_session(self) -> None:
        class EmptyCatalog:
            def catalog(self):
                return ()

        class FakeRuntime:
            def __init__(self) -> None:
                self.skills = EmptyCatalog()
                self.tasks: list[str] = []

            async def run_task(self, task, session_id=None):
                self.tasks.append(task)
                if False:
                    yield session_id

            async def close(self):
                return None

        runtime = FakeRuntime()
        with (
            patch("run_ui.cli.load_runtime_config", return_value=object()),
            patch("run_ui.cli.AgentRuntime", return_value=runtime),
            patch("run_ui.cli.console.input", side_effect=["/skill list", "/exit"]),
        ):
            asyncio.run(_chat(None))
        self.assertEqual(runtime.tasks, [])


if __name__ == "__main__":
    unittest.main()
