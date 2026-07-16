"""Skills/Marketplace/Plugin 安装事务与链接边界的无网络回归测试。

所有 Git 来源均为测试临时目录中的本地仓库；Marketplace 和插件也只使用
本地相对来源。测试不会访问真实用户目录、网络、npm 或全局 Git 配置。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Agent.config import RuntimeConfig
from Agent.storage import StateStore
from skills.registry import PluginManager, SkillInstaller


def _write_json(path: Path, value: object) -> None:
    """创建父目录并写入测试用 UTF-8 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_skill(path: Path, *, description: str, body: str) -> None:
    """写入一个最小、符合 Open Agent Skills frontmatter 的技能。"""

    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: sample-skill\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )


def _git(repo: Path, *arguments: str) -> None:
    """在本地测试仓库执行 Git 命令，失败时直接显示 stderr。"""

    subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _create_marketplace(root: Path, *, payload: str = "version-one") -> Path:
    """创建包含一个本地相对来源插件的最小 Marketplace。"""

    plugin = root / "plugins" / "sample"
    _write_json(
        root / ".claude-plugin" / "marketplace.json",
        {"plugins": [{"name": "sample", "source": "./plugins/sample", "version": "1.0.0"}]},
    )
    _write_json(plugin / ".claude-plugin" / "plugin.json", {"name": "sample", "version": "1.0.0"})
    (plugin / "payload.txt").parent.mkdir(parents=True, exist_ok=True)
    (plugin / "payload.txt").write_text(payload, encoding="utf-8")
    return plugin


def _create_directory_link(link: Path, target: Path) -> None:
    """创建目录 symlink；Windows 无相应权限时退化为无需管理员的 junction。"""

    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (NotImplementedError, OSError):
        if os.name != "nt":
            raise
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        raise OSError(result.stderr or result.stdout or "无法创建测试 junction")


def _remove_directory_link(link: Path) -> None:
    """只删除 symlink/junction 目录项本身，不递归触碰其目标。"""

    try:
        link.unlink()
    except OSError:
        link.rmdir()


class SkillRegistrySecurityTests(unittest.TestCase):
    """验证不可信扩展内容永远先校验 staging，失败时保留旧状态。"""

    def setUp(self) -> None:
        """为每个用例隔离项目、用户目录和 SQLite 状态。"""

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.home = self.root / "home"
        self.environment = patch.dict(os.environ, {"YY_AGENT_HOME": str(self.home)}, clear=False)
        self.environment.start()
        self.config = RuntimeConfig(project_root=self.project)
        self.store = StateStore(self.config.state_db)

    def tearDown(self) -> None:
        """恢复环境变量并清理全部临时安装。"""

        self.environment.stop()
        self.temporary.cleanup()

    def test_git_skill_failed_update_preserves_directory_and_lock(self) -> None:
        """Git Skill 校验或最终 lock 写入失败都不能破坏当前安装。"""

        if shutil.which("git") is None:
            self.skipTest("测试环境没有 Git")
        repo = self.root / "skill-repo"
        repo.mkdir()
        _git(repo, "init")
        _git(repo, "config", "user.email", "tests@example.invalid")
        _git(repo, "config", "user.name", "yy-agent tests")
        _write_skill(repo, description="Safe version one.", body="old-body")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "version one")

        installer = SkillInstaller(self.config)
        installed = installer.add(str(repo))
        old_text = (installed.path / "SKILL.md").read_text(encoding="utf-8")
        old_locks = (self.config.yy_dir / "skills.lock.json").read_bytes()

        # 上游损坏时在来源验证阶段失败，正式目录和锁文件都必须保持原样。
        (repo / "SKILL.md").write_text("not valid frontmatter\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "broken version")
        with self.assertRaises(ValueError):
            installer.update("sample-skill")
        self.assertEqual((installed.path / "SKILL.md").read_text(encoding="utf-8"), old_text)
        self.assertEqual((self.config.yy_dir / "skills.lock.json").read_bytes(), old_locks)

        # 即使 staging 已完整验证，最后状态写入失败也必须把旧目录移回原位。
        _write_skill(repo, description="Safe version two.", body="new-body")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "version two")
        with patch.object(installer, "_write_lock", side_effect=OSError("simulated lock failure")):
            with self.assertRaises(OSError):
                installer.update("sample-skill")
        self.assertEqual((installed.path / "SKILL.md").read_text(encoding="utf-8"), old_text)
        self.assertEqual((self.config.yy_dir / "skills.lock.json").read_bytes(), old_locks)

    def test_git_skill_rejects_tracked_symlink_before_copy(self) -> None:
        """支持原生 symlink 的平台上，Git Skill 链接不能被复制后洗白。"""

        if shutil.which("git") is None:
            self.skipTest("测试环境没有 Git")
        repo = self.root / "linked-skill-repo"
        repo.mkdir()
        _git(repo, "init")
        _git(repo, "config", "user.email", "tests@example.invalid")
        _git(repo, "config", "user.name", "yy-agent tests")
        _write_skill(repo, description="Contains a forbidden link.", body="body")
        external = self.root / "outside.txt"
        external.write_text("secret", encoding="utf-8")
        try:
            (repo / "linked.txt").symlink_to(external)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"当前平台不能创建可由 Git 保留的 symlink：{exc}")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "linked skill")

        with self.assertRaises(ValueError):
            SkillInstaller(self.config).add(str(repo))
        self.assertFalse((self.config.yy_dir / "skills" / "sample-skill").exists())
        self.assertFalse((self.config.yy_dir / "skills.lock.json").exists())

    def test_local_marketplace_invalid_replacement_preserves_registered_copy(self) -> None:
        """本地 Marketplace 新副本校验失败时保留旧目录和注册表。"""

        source = self.root / "market-source"
        _create_marketplace(source)
        manager = PluginManager(self.config, self.store)
        manager.add_marketplace(str(source), "local-market")
        registered = manager.marketplaces()["local-market"]
        target = Path(registered["path"])
        old_catalog = (target / ".claude-plugin" / "marketplace.json").read_bytes()
        old_registry = manager.marketplaces_file.read_bytes()

        _write_json(source / ".claude-plugin" / "marketplace.json", {"plugins": "invalid"})
        with self.assertRaises(ValueError):
            manager.update_marketplace("local-market")
        self.assertEqual((target / ".claude-plugin" / "marketplace.json").read_bytes(), old_catalog)
        self.assertEqual(manager.marketplaces_file.read_bytes(), old_registry)

    def test_plugin_update_preserves_or_resets_trust_exactly_with_content(self) -> None:
        """相同内容保留信任，内容变化才清空并准确报告 trust_reset。"""

        source = self.root / "market-source"
        _create_marketplace(source)
        manager = PluginManager(self.config, self.store)
        manager.add_marketplace(str(source), "local-market")
        first = manager.install("sample@local-market")
        self.assertEqual(first["trusted_components"], [])
        self.assertFalse(first["trust_reset"])
        manager.trust("sample@local-market", ["scripts", "hooks"])

        unchanged = manager.update("sample@local-market")
        self.assertFalse(unchanged["changed"])
        self.assertFalse(unchanged["trust_reset"])
        self.assertEqual(unchanged["trusted_components"], ["scripts", "hooks"])
        row = manager.installed()[0]
        self.assertEqual(json.loads(row["trusted_components_json"]), ["scripts", "hooks"])

        market_copy = Path(manager.marketplaces()["local-market"]["path"])
        (market_copy / "plugins" / "sample" / "payload.txt").write_text("version-two", encoding="utf-8")
        changed = manager.update("sample@local-market")
        self.assertTrue(changed["changed"])
        self.assertTrue(changed["trust_reset"])
        self.assertEqual(changed["trusted_components"], [])
        self.assertEqual(json.loads(manager.installed()[0]["trusted_components_json"]), [])
        self.assertEqual((Path(changed["path"]) / "payload.txt").read_text(encoding="utf-8"), "version-two")

    def test_plugin_state_write_failure_restores_previous_cache(self) -> None:
        """SQLite upsert 失败时，已换入的新插件树必须回滚为旧缓存。"""

        source = self.root / "market-source"
        _create_marketplace(source)
        manager = PluginManager(self.config, self.store)
        manager.add_marketplace(str(source), "local-market")
        installed = manager.install("sample@local-market")
        manager.trust("sample@local-market", ["scripts"])
        installed_path = Path(installed["path"])
        old_payload = (installed_path / "payload.txt").read_bytes()
        old_state = manager.installed()[0]

        market_copy = Path(manager.marketplaces()["local-market"]["path"])
        (market_copy / "plugins" / "sample" / "payload.txt").write_text("candidate", encoding="utf-8")
        with patch.object(self.store, "execute", side_effect=OSError("simulated sqlite failure")):
            with self.assertRaises(OSError):
                manager.update("sample@local-market")
        self.assertEqual((installed_path / "payload.txt").read_bytes(), old_payload)
        current_state = manager.installed()[0]
        self.assertEqual(current_state["content_hash"], old_state["content_hash"])
        self.assertEqual(json.loads(current_state["trusted_components_json"]), ["scripts"])

    def test_links_are_rejected_before_local_marketplace_and_plugin_copy(self) -> None:
        """Marketplace 与其插件源中的链接不能被 copytree 解引用后洗白。"""

        external = self.root / "outside-secret"
        external.mkdir()
        (external / "secret.txt").write_text("must-not-be-copied", encoding="utf-8")
        source = self.root / "market-source"
        _create_marketplace(source)
        market_link = source / "linked-secret"
        try:
            _create_directory_link(market_link, external)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"当前平台不能创建测试 symlink/junction：{exc}")

        manager = PluginManager(self.config, self.store)
        with self.assertRaises(ValueError):
            manager.add_marketplace(str(source), "unsafe-market")
        self.assertNotIn("unsafe-market", manager.marketplaces())
        self.assertFalse((self.home / "plugins" / "marketplaces" / "unsafe-market").exists())

        # 注册一个无链接的安全副本，再在已注册 Marketplace 中植入链接，验证插件安装
        # 自身也会重新检查来源，而不是依赖 Marketplace 注册时的一次性结论。
        _remove_directory_link(market_link)
        manager.add_marketplace(str(source), "local-market")
        installed = manager.install("sample@local-market")
        manager.trust("sample@local-market", ["scripts"])
        old_payload = (Path(installed["path"]) / "payload.txt").read_bytes()
        old_state = manager.installed()[0]
        market_copy = Path(manager.marketplaces()["local-market"]["path"])
        plugin_link = market_copy / "plugins" / "sample" / "linked-secret"
        _create_directory_link(plugin_link, external)
        with self.assertRaises(ValueError):
            manager.update("sample@local-market")
        self.assertEqual((Path(installed["path"]) / "payload.txt").read_bytes(), old_payload)
        current_state = manager.installed()[0]
        self.assertEqual(current_state["content_hash"], old_state["content_hash"])
        self.assertEqual(json.loads(current_state["trusted_components_json"]), ["scripts"])


if __name__ == "__main__":
    unittest.main()
