"""Open Agent Skills 发现、固定 Git 安装与 Claude 风格插件市场管理。

运行时只常驻 Skill 的名称和描述，被选中后才渐进加载
``SKILL.md``。安装流程先在临时目录物化来源，验证 frontmatter、
路径逃逸和符号链接，再复制到项目或用户作用域，并记录来源、
Git SHA 与内容哈希供更新审计。

插件市场同时识别 ``.yy-plugin`` 和 ``.claude-plugin`` 约定。
安装插件只代表下载和登记，脚本、Hooks、MCP、LSP 和 Agents 等
可执行组件需要单独信任；内容发生变化时信任集会重置。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import yaml
except ImportError:
    # PyYAML 是可选依赖；缺失时仍保留支持基础 frontmatter 的核心发现能力。
    yaml = None

from Agent.config import RuntimeConfig
from Agent.storage import StateStore
from Agent.types import utc_now


# 名称规则对齐 Open Agent Skills：小写 kebab-case，最长 64 字符，不允许连字符收尾。
SKILL_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


@dataclass(frozen=True)
class Skill:
    """一个已验证 Skill 的轻量不可变元数据。

    ``path`` 指向包含 ``SKILL.md`` 的目录；``scope`` 记录来源层级；
    插件技能通过 ``namespace`` 防止不同插件的同名 Skill 互相覆盖。
    对象冻结可避免发现后的元数据被下游悄然改写。
    """

    name: str
    description: str
    path: Path
    scope: str
    namespace: str | None = None
    metadata: dict[str, Any] | None = None

    @property
    def qualified_name(self) -> str:
        """返回用于注册和调用的唯一名称。"""

        return f"{self.namespace}:{self.name}" if self.namespace else self.name

    def load(self) -> str:
        """渐进读取 Skill 指令主体，不将 YAML frontmatter 交给模型。"""

        _, body = parse_frontmatter((self.path / "SKILL.md").read_text(encoding="utf-8"))
        return body.strip()


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """拆分 Markdown 顶部 YAML frontmatter 与指令主体。

    frontmatter 必须从文件第一个 ``---`` 开始并有对应闭合线；
    宽松搜索后续分隔符会让普通 Markdown 内容被误当作元数据。
    有 PyYAML 时使用完整解析，否则退化到只覆盖常用顶层标量的
    :func:`_minimal_yaml`。
    """

    if not text.startswith("---"):
        raise ValueError("Markdown 缺少 YAML frontmatter")
    match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?(.*)$", text, re.DOTALL)
    if not match:
        raise ValueError("frontmatter 未正确闭合")
    metadata = (yaml.safe_load(match.group(1)) if yaml is not None else _minimal_yaml(match.group(1))) or {}
    if not isinstance(metadata, dict):
        raise ValueError("frontmatter 必须是映射")
    return metadata, match.group(2)


def _minimal_yaml(value: str) -> dict[str, Any]:
    """在未安装 PyYAML 时解析简单的顶层 ``key: value`` 元数据。

    该降级解析器故意忽略缩进行，不尝试实现完整 YAML；支持布尔值、
    内联列表与 :func:`ast.literal_eval` 可安全处理的字面量。
    它从不使用 ``eval``，因此元数据无法执行 Python 代码。
    """

    result: dict[str, Any] = {}
    for raw_line in value.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#") or raw_line.startswith((" ", "\t")):
            continue
        key, marker, raw = raw_line.partition(":")
        if not marker:
            continue
        raw = raw.strip()
        if raw.lower() in {"true", "false"}:
            parsed: Any = raw.lower() == "true"
        elif raw.startswith("[") and raw.endswith("]"):
            items = raw[1:-1].strip()
            parsed = [item.strip().strip("'\"") for item in items.split(",") if item.strip()]
        else:
            try:
                # literal_eval 只允许 Python 字面量，不会执行函数或属性访问。
                parsed = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                parsed = raw.strip("'\"")
        result[key.strip()] = parsed
    return result


def validate_skill(path: Path) -> Skill:
    """校验 Skill 入口和 Open Agent Skills 所需的核心元数据。

    必须存在 ``SKILL.md``，``name`` 需满足规范，``description`` 长度
    为 1–1024 字符。此函数只返回结构化索引对象，不执行 Skill
    目录中的 scripts，也不会隐式信任其可执行内容。
    """

    entry = path / "SKILL.md"
    if not entry.exists():
        raise ValueError(f"技能缺少 SKILL.md：{path}")
    metadata, _ = parse_frontmatter(entry.read_text(encoding="utf-8"))
    name = str(metadata.get("name", path.name))
    description = str(metadata.get("description", "")).strip()
    if not SKILL_NAME.fullmatch(name):
        raise ValueError(f"技能名称不符合规范：{name}")
    if not description or len(description) > 1024:
        raise ValueError("技能 description 必须为 1-1024 个字符")
    return Skill(name, description, path, "unknown", metadata=metadata)


class SkillRegistry:
    """按优先级发现用户、兼容、项目和插件作用域中的 Skills。

    发现阶段只验证入口并常驻名称/描述，不会预先读取 references、
    assets 或 scripts。同名非插件 Skill 按根目录遍历顺序由后者覆盖，
    使项目级 ``.yy/skills`` 能够覆盖只读兼容目录；插件 Skill 则使用
    ``namespace:name`` 隔离。
    """

    def __init__(self, config: RuntimeConfig) -> None:
        """绑定已解析的运行时路径配置。"""

        self.config = config
        self._skills: dict[str, Skill] = {}

    def discover(self, plugin_roots: list[Path] | None = None) -> list[Skill]:
        """重建 Skill 索引并返回当前可发现的全部技能。

        损坏或不符合规范的单个 Skill 会被跳过，避免一个库的错误
        让整个 Agent 无法启动。插件 manifest 错误仍会向上抛出，因为
        无法确定命名空间时悄然加载可能导致冲突。
        """

        self._skills = {}
        roots = [
            (self.config.user_dir / "skills", "user", None),
            (self.config.project_root / ".claude" / "skills", "compat", None),
            (self.config.project_root / ".agents" / "skills", "compat", None),
            (self.config.project_root / ".yy" / "skills", "project", None),
        ]
        for root in plugin_roots or []:
            manifest = load_plugin_manifest(root)
            namespace = str(manifest.get("name", root.name))
            roots.append((root / "skills", "plugin", namespace))
        for root, scope, namespace in roots:
            if not root.exists():
                continue
            for path in root.iterdir():
                if not path.is_dir() or not (path / "SKILL.md").exists():
                    continue
                try:
                    skill = validate_skill(path)
                except ValueError:
                    # 发现对单个目录容错；CLI 可用 validate 单独显示具体错误。
                    continue
                skill = Skill(skill.name, skill.description, path, scope, namespace, skill.metadata)
                self._skills[skill.qualified_name] = skill
        return list(self._skills.values())

    def get(self, name: str) -> Skill | None:
        """按普通名或带命名空间的限定名查找已发现 Skill。"""

        return self._skills.get(name)

    def catalog(self) -> str:
        """生成仅含名称和描述的精简 Prompt 目录。"""

        return "\n".join(f"- {skill.qualified_name}: {skill.description}" for skill in self._skills.values())


class SkillInstaller:
    """从 Git 来源安装、更新和删除项目或用户级 Skill。

    来源可以使用 ``<git-url>#<subdir>`` 选择单仓库子目录，
    ``ref`` 可固定 tag、branch 或 commit SHA。安装锁记录原始来源、
    请求 ref、实际 commit 和内容哈希，使更新可重现并可审计。
    """

    def __init__(self, config: RuntimeConfig) -> None:
        """使用运行时配置确定项目和用户安装根。"""

        self.config = config

    def add(self, source: str, *, ref: str | None = None, scope: str = "project", overwrite: bool = False) -> Skill:
        """从 Git 来源安装一个已验证 Skill。

        先在系统临时目录 clone，子目录必须仍位于 checkout 真实路径下，
        且来源树中不允许出现任何符号链接或 Windows 重解析点。
        如果选定目录没有直接包含 ``SKILL.md``，只在存在唯一一级候选时
        自动选择，避免安装错误技能。通过校验的内容先复制到目标同级 staging；
        staging 的 Skill 和内容哈希均验证完成后才替换正式目录。覆盖安装期间会
        暂存旧目录，目录替换或 lock 写入失败都会恢复旧安装。
        """

        if scope not in {"project", "user"}:
            raise ValueError("scope 必须是 project 或 user")
        url, separator, subdir = source.partition("#")
        with tempfile.TemporaryDirectory(prefix="yy-skill-") as temporary:
            checkout = Path(temporary) / "repo"
            _clone(url, checkout, ref)
            selected = checkout / subdir if separator else checkout
            resolved = _resolve_confined_source(checkout, selected, label="skill subdir")
            # 必须在读取 SKILL.md 之前扫描来源树。copytree 默认会解引用链接，
            # 若等复制后再检查，恶意链接早已变成看似普通的外部文件副本。
            _assert_tree_confined(resolved)
            if not (resolved / "SKILL.md").exists():
                # 仅探测一级目录，防止在任意深层树中“猜测”用户意图。
                candidates = list(resolved.glob("*/SKILL.md"))
                if len(candidates) != 1:
                    raise ValueError("仓库根目录没有唯一可识别的 SKILL.md")
                resolved = candidates[0].parent
            skill = validate_skill(resolved)
            commit = _git_sha(checkout)
            target_root = self.config.yy_dir / "skills" if scope == "project" else self.config.user_dir / "skills"
            target = target_root / skill.name
            if _path_exists(target):
                if not overwrite:
                    raise FileExistsError(f"技能已安装：{skill.name}")
            target_root.mkdir(parents=True, exist_ok=True)
            with _staging_tree(target) as staging:
                _copy_tree_checked(resolved, staging)
                installed = validate_skill(staging)
                content_hash = _tree_hash(staging)
                lock_value = {
                    "source": source, "ref": ref, "commit": commit, "content_hash": content_hash,
                    "installed_path": str(target), "updated_at": utc_now(),
                }
                _commit_staged_tree(
                    staging,
                    target,
                    finalize=lambda: self._write_lock(scope, installed.name, lock_value),
                )
            return Skill(installed.name, installed.description, target, scope, metadata=installed.metadata)

    def update(self, name: str, *, scope: str = "project") -> Skill:
        """使用锁记录中的原始来源和 ref 重新安装 Skill。

        更新不接受临时替换来源，避免同一名称在审计记录中悄然
        换成另一个仓库。
        """

        lock = self._locks(scope).get(name)
        if not lock:
            raise KeyError(f"技能没有安装锁记录：{name}")
        return self.add(str(lock["source"]), ref=lock.get("ref"), scope=scope, overwrite=True)

    def remove(self, name: str, *, scope: str = "project") -> None:
        """删除作用域内的 Skill 目录与对应锁记录。

        真实目标必须是安装根的子路径，否则在调用 ``rmtree`` 前就失败。
        """

        target_root = self.config.yy_dir / "skills" if scope == "project" else self.config.user_dir / "skills"
        target = (target_root / name).resolve()
        target.relative_to(target_root.resolve())
        if not target.exists():
            raise KeyError(name)
        shutil.rmtree(target)
        locks = self._locks(scope)
        locks.pop(name, None)
        self._save_locks(scope, locks)

    def _lock_path(self, scope: str) -> Path:
        """返回指定安装作用域的 JSON 锁文件路径。"""

        return (self.config.yy_dir / "skills.lock.json") if scope == "project" else (self.config.user_dir / "skills.lock.json")

    def _locks(self, scope: str) -> dict[str, Any]:
        """读取锁文件；缺失或非映射根值按空集合处理。"""

        path = self._lock_path(scope)
        if not path.exists():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}

    def _save_locks(self, scope: str, locks: dict[str, Any]) -> None:
        """以稳定、人类可审计的 UTF-8 JSON 格式原子保存锁记录。

        临时文件与目标位于同一目录，写入、``fsync`` 和 ``os.replace``
        完成后读者只会看到旧版或新版完整 JSON，不会看到半写入状态。
        """

        path = self._lock_path(scope)
        _write_json_atomic(path, locks)

    def _write_lock(self, scope: str, name: str, value: dict[str, Any]) -> None:
        """合并单个 Skill 锁项，不覆盖同作用域的其他安装记录。"""

        locks = self._locks(scope)
        locks[name] = value
        self._save_locks(scope, locks)


def load_plugin_manifest(root: Path) -> dict[str, Any]:
    """读取原生 ``.yy-plugin`` 或兼容 ``.claude-plugin`` manifest。

    原生格式优先；找不到 manifest 时使用目录名作为只读发现的
    命名空间。一旦 manifest 存在，根必须是包含 ``name`` 的 JSON 对象，
    不会对损坏声明静默降级。
    """

    candidates = [root / ".yy-plugin" / "plugin.json", root / ".claude-plugin" / "plugin.json"]
    for path in candidates:
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not value.get("name"):
                raise ValueError(f"无效插件 manifest：{path}")
            return value
    return {"name": root.name, "version": None}


class PluginManager:
    """管理本地插件市场、安装缓存与可执行组件信任状态。

    Marketplace 目录保存可更新的 catalog，cache 保存按市场/插件/版本
    物化的内容，SQLite ``plugin_state`` 则是启用、哈希和信任状态的权威记录。
    安装默认将 ``trusted_components`` 置空；文本 Skills 可被发现，但脚本、
    Hooks、MCP、LSP 和 Agents 必须由用户逐项信任。
    """

    def __init__(self, config: RuntimeConfig, store: StateStore) -> None:
        """绑定运行时路径和持久化状态库。"""

        self.config = config
        self.store = store
        self.marketplaces_file = config.user_dir / "plugins" / "known_marketplaces.json"
        self.cache = config.user_dir / "plugins" / "cache"

    def marketplaces(self) -> dict[str, dict[str, Any]]:
        """读取已登记 Marketplace 映射，未配置时返回空字典。"""

        if not self.marketplaces_file.exists():
            return {}
        value = json.loads(self.marketplaces_file.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}

    def add_marketplace(self, source: str, name: str | None = None) -> str:
        """从本地目录、GitHub ``owner/repo`` 或 Git URL 登记一个市场。

        市场名必须符合 Skill 名规范，因为它会进入 ``plugin@marketplace``
        稳定标识。来源先物化到目标同级 staging，拒绝其中任何链接，再校验
        catalog；只有 staging 和注册表 JSON 都可完整提交时才替换旧市场。
        因此损坏的本地目录或 clone/校验失败不会删除当前可用的市场副本。
        """

        markets = self.marketplaces()
        inferred = name or Path(source.rstrip("/")).stem.replace(".git", "")
        if not SKILL_NAME.fullmatch(inferred):
            raise ValueError("marketplace 名称必须是 kebab-case")
        target = self.config.user_dir / "plugins" / "marketplaces" / inferred
        with _staging_tree(target) as staging:
            local_source = Path(source)
            if local_source.exists():
                _copy_tree_checked(local_source, staging)
            else:
                url = f"https://github.com/{source}.git" if re.fullmatch(r"[^/]+/[^/]+", source) else source
                _run(["git", "clone", "--filter=blob:none", "--depth", "1", url, str(staging)])
                # Git checkout 也属于不可信来源；clone 完成后、读取 catalog 前拒绝链接。
                _assert_tree_confined(staging)
            # 先物化再校验，不从远程 URL 直接解析不可复现的流式内容。
            catalog = _find_marketplace_catalog(staging)
            _validate_marketplace(catalog)
            markets[inferred] = {"source": source, "path": str(target), "updated_at": utc_now()}
            _commit_staged_tree(
                staging,
                target,
                finalize=lambda: _write_json_atomic(self.marketplaces_file, markets),
            )
        return inferred

    def remove_marketplace(self, name: str) -> None:
        """卸载该市场提供的插件，并删除本地 catalog 副本。

        删除路径在 ``rmtree`` 前必须证明位于固定 marketplaces 根下；
        这可防止被篡改的市场记录把删除导向用户目录的其他位置。
        """

        markets = self.marketplaces()
        entry = markets.pop(name, None)
        if not entry:
            raise KeyError(name)
        for plugin in list(self.installed()):
            if str(plugin["id"]).endswith(f"@{name}"):
                self.uninstall(str(plugin["id"]))
        path = Path(entry["path"]).resolve()
        expected = (self.config.user_dir / "plugins" / "marketplaces").resolve()
        path.relative_to(expected)
        if path.exists():
            shutil.rmtree(path)
        self.marketplaces_file.write_text(json.dumps(markets, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def update_marketplace(self, name: str) -> None:
        """从登记来源重新 staging、校验并替换 Marketplace。

        不在当前已安装目录中执行 ``git pull``，因为 pull 成功、catalog 校验失败
        会留下不可用的半更新市场。重新物化会让本地目录与 Git 来源共用同一条
        事务式路径，任何失败都保留旧目录和旧注册表记录。
        """

        entry = self.marketplaces().get(name)
        if not entry:
            raise KeyError(name)
        self.add_marketplace(str(entry["source"]), name)

    def catalog(self, marketplace: str) -> list[dict[str, Any]]:
        """返回指定已登记市场中的插件条目列表。"""

        entry = self.marketplaces().get(marketplace)
        if not entry:
            raise KeyError(marketplace)
        value = json.loads(_find_marketplace_catalog(Path(entry["path"])).read_text(encoding="utf-8"))
        return list(value.get("plugins", []))

    def install(self, identifier: str, *, scope: str = "project") -> dict[str, Any]:
        """将 ``plugin@marketplace`` 安全物化到缓存并登记信任状态。

        catalog 中的名称必须唯一匹配。内容根据本地、GitHub、Git URL、
        Git 子目录或 npm 来源物化到 staging，随后拒绝链接、加载 manifest 并
        计算确定性内容哈希。首次安装的信任组件为空；再次安装相同字节时保留
        现有信任，内容变化时才清空，且返回的 ``trust_reset`` 与实际状态一致。
        staging 和单语句 SQLite upsert 共同提交，任何失败都会恢复旧缓存目录。
        """

        plugin_name, marker, marketplace = identifier.partition("@")
        if not marker:
            raise ValueError("插件标识必须是 plugin@marketplace")
        entries = [item for item in self.catalog(marketplace) if item.get("name") == plugin_name]
        if len(entries) != 1:
            raise KeyError(identifier)
        entry = entries[0]
        market_root = Path(self.marketplaces()[marketplace]["path"])
        version = str(entry.get("version") or "git")
        if version in {"", ".", ".."} or "/" in version or "\\" in version:
            raise ValueError("插件 version 必须是单个安全路径组件")
        target = self.cache / marketplace / plugin_name / version
        plugin_id = f"{plugin_name}@{marketplace}"
        previous_rows = self.store.query("SELECT * FROM plugin_state WHERE id=?", (plugin_id,))
        previous = previous_rows[0] if previous_rows else None
        with _staging_tree(target) as staging:
            self._materialize_source(entry["source"], market_root, staging)
            _assert_tree_confined(staging)
            manifest = load_plugin_manifest(staging)
            # 哈希覆盖相对路径与内容，既能检测修改，也能检测文件重命名。
            digest = _tree_hash(staging)
            commit = _git_sha(staging) or entry.get("sha")
            changed = previous is not None and previous["content_hash"] != digest
            try:
                previous_trusted = json.loads(previous["trusted_components_json"]) if previous else []
            except (TypeError, json.JSONDecodeError):
                previous_trusted = []
            if not isinstance(previous_trusted, list):
                # 数据库字段虽由本类写入，仍按不可信持久化输入处理，损坏值不继承授权。
                previous_trusted = []
            trusted_components = [] if previous is None or changed else list(previous_trusted)
            trust_reset = bool(changed and previous_trusted)
            enabled = int(previous["enabled"]) if previous else 1
            statement = """
                INSERT INTO plugin_state(
                    id,scope,source,version,commit_sha,content_hash,enabled,
                    trusted_components_json,installed_path,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    scope=excluded.scope,source=excluded.source,version=excluded.version,
                    commit_sha=excluded.commit_sha,content_hash=excluded.content_hash,
                    enabled=excluded.enabled,
                    trusted_components_json=excluded.trusted_components_json,
                    installed_path=excluded.installed_path,updated_at=excluded.updated_at
            """
            values = (
                plugin_id, scope, json.dumps(entry["source"], ensure_ascii=False),
                manifest.get("version") or entry.get("version"), commit, digest, enabled,
                json.dumps(trusted_components), str(target), utc_now(),
            )
            # upsert 是单个 SQLite 事务；若它失败，目录提交器会把旧缓存改回原位。
            _commit_staged_tree(
                staging,
                target,
                finalize=lambda: self.store.execute(statement, values),
            )
        return {
            "id": plugin_id,
            "path": str(target),
            "hash": digest,
            "trusted_components": trusted_components,
            "trust_reset": trust_reset,
        }

    def set_enabled(self, identifier: str, enabled: bool) -> None:
        """启用或禁用已安装插件，不删除缓存或信任记录。"""

        self.store.execute("UPDATE plugin_state SET enabled=?,updated_at=? WHERE id=?", (int(enabled), utc_now(), identifier))

    def update(self, identifier: str) -> dict[str, Any]:
        """从市场重新安装插件，并报告内容是否变化。

        ``changed`` 用前后内容哈希表示字节是否变化。相同内容保留原有
        ``trusted_components`` 并返回 ``trust_reset=False``；内容变化时
        :meth:`install` 清空已有信任，只有确实撤销过非空信任集时才返回
        ``trust_reset=True``，避免 UI 状态与 SQLite 实际值相互矛盾。
        """

        rows = self.store.query("SELECT scope,content_hash FROM plugin_state WHERE id=?", (identifier,))
        if not rows:
            raise KeyError(identifier)
        before = rows[0]["content_hash"]
        result = self.install(identifier, scope=str(rows[0]["scope"]))
        result["changed"] = result["hash"] != before
        return result

    def trust(self, identifier: str, components: list[str]) -> None:
        """替换插件已显式信任的可执行组件集合。

        只允许固定组件类型，不接受任意路径或命令。这个记录仅表示
        用户信任该类组件，实际运行脚本、Hook、MCP 或 LSP 时仍受沙箱和
        能力审批约束。
        """

        allowed = {"scripts", "hooks", "mcp", "lsp", "agents"}
        if not set(components) <= allowed:
            raise ValueError(f"未知可执行组件，可选：{', '.join(sorted(allowed))}")
        self.store.execute("UPDATE plugin_state SET trusted_components_json=?,updated_at=? WHERE id=?", (json.dumps(components), utc_now(), identifier))

    def installed(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        """列出全部或仅启用的插件持久化状态。"""

        sql = "SELECT * FROM plugin_state" + (" WHERE enabled=1" if enabled_only else "")
        return self.store.query(sql + " ORDER BY id")

    def uninstall(self, identifier: str) -> None:
        """删除插件缓存目录和 SQLite 状态记录。

        数据库中的 ``installed_path`` 被视为不可信持久化输入，因此
        在递归删除前必须通过真实路径检查确认位于插件 cache 下。
        """

        rows = self.store.query("SELECT installed_path FROM plugin_state WHERE id=?", (identifier,))
        if not rows:
            raise KeyError(identifier)
        path = Path(rows[0]["installed_path"]).resolve()
        path.relative_to(self.cache.resolve())
        if path.exists():
            shutil.rmtree(path)
        self.store.execute("DELETE FROM plugin_state WHERE id=?", (identifier,))

    def _materialize_source(self, source: Any, market_root: Path, target: Path) -> None:
        """将 Marketplace 条目声明的来源物化到独立目标目录。

        字符串来源必须以 ``./`` 开头且真实路径位于 Marketplace 根内。
        结构化来源支持 GitHub、通用 Git URL、Git 子目录和 npm。Git 来源可通过
        ref/SHA 固定；npm 始终使用 ``--ignore-scripts``，下载阶段绝不执行包的
        lifecycle scripts。所有需要复制的本地树都会在复制前拒绝链接，并使用
        ``symlinks=True`` 保留竞争窗口中新出现的链接，以便复制后校验能够发现，
        而不是让 ``copytree`` 悄然解引用到树外内容。
        """

        if isinstance(source, str):
            if not source.startswith("./"):
                raise ValueError("相对插件源必须以 ./ 开头")
            source_path = _resolve_confined_source(market_root, market_root / source, label="插件相对源")
            _copy_tree_checked(source_path, target)
            return
        if not isinstance(source, dict):
            raise ValueError("无效插件 source")
        kind = source.get("source") or source.get("type")
        if kind == "github":
            url = f"https://github.com/{source['repo']}.git"
            _clone(url, target, source.get("ref") or source.get("sha"))
        elif kind == "url":
            _clone(str(source["url"]), target, source.get("ref") or source.get("sha"))
        elif kind == "git-subdir":
            with tempfile.TemporaryDirectory(prefix="yy-plugin-") as temporary:
                repo = Path(temporary) / "repo"
                _clone(str(source["url"]), repo, source.get("ref") or source.get("sha"))
                subdir = _resolve_confined_source(repo, repo / str(source["path"]), label="插件 Git subdir")
                _copy_tree_checked(subdir, target)
        elif kind == "npm":
            target.mkdir(parents=True)
            # 安装阶段禁用 npm lifecycle scripts，可执行组件必须在安装后另行信任。
            _run(["npm", "install", "--ignore-scripts", "--prefix", str(target), f"{source['package']}@{source.get('version', 'latest')}"])
            packages = list((target / "node_modules").glob(str(source["package"])))
            if len(packages) != 1:
                raise RuntimeError("无法定位 npm 插件包")
            package = packages[0]
            # npm 工作树仍位于外层事务 staging 内。先把实际包安全复制到同级临时树，
            # 完整检查后再替换工作树，最终 cache 不保留 node_modules 包装目录。
            package_staging = target.with_name(target.name + ".package")
            _copy_tree_checked(package, package_staging)
            _remove_path(target)
            package_staging.replace(target)
        else:
            raise ValueError(f"不支持的插件源：{kind}")


def _path_exists(path: Path) -> bool:
    """不跟随链接判断目录项是否存在，连损坏 symlink 也视为已存在。"""

    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _is_link_like(path: Path) -> bool:
    """识别 POSIX symlink 与 Windows junction/其他重解析点。"""

    metadata = path.lstat()
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return path.is_symlink() or bool(reparse_flag and attributes & reparse_flag)


def _resolve_confined_source(root: Path, candidate: Path, *, label: str) -> Path:
    """解析来源子路径，同时拒绝 ``..`` 逃逸和路径组件中的链接。

    只比较最终 ``resolve`` 结果会漏掉“指向根内另一处”的 symlink；虽然它没有
    越界，仍违反安装包不允许链接的不变量。因此先沿词法相对路径逐组件检查，
    再验证最终真实路径仍位于可信根下。返回值一定存在且是解析后的路径。
    """

    root_absolute = root.absolute()
    candidate_absolute = candidate.absolute()
    if not _path_exists(root_absolute) or _is_link_like(root_absolute):
        raise ValueError(f"{label} 的根目录不存在或是链接：{root}")
    try:
        relative = candidate_absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise ValueError(f"{label} 不能逃逸来源根目录") from exc
    cursor = root_absolute
    for component in relative.parts:
        cursor = cursor / component
        if _path_exists(cursor) and _is_link_like(cursor):
            raise ValueError(f"{label} 包含符号链接或重解析点：{cursor}")
    if not _path_exists(candidate_absolute):
        raise ValueError(f"{label} 不存在：{candidate}")
    resolved_root = root_absolute.resolve(strict=True)
    resolved = candidate_absolute.resolve(strict=True)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label} 不能逃逸来源根目录") from exc
    return resolved


def _copy_tree_checked(source: Path, target: Path) -> None:
    """在复制前后都拒绝链接，并把来源树复制到全新 staging 路径。

    ``shutil.copytree`` 默认 ``symlinks=False``，会跟随链接并把树外文件复制成
    普通文件，导致复制后的检查失去证据。这里先检查原树，再显式使用
    ``symlinks=True``；若检查与复制之间出现链接，它会被保留到 staging，随后
    第二次检查会拒绝。目标必须不存在，避免不可信内容与旧树合并。
    """

    _assert_tree_confined(source)
    if _path_exists(target):
        raise FileExistsError(f"staging 目标已存在：{target}")
    shutil.copytree(source, target, symlinks=True)
    _assert_tree_confined(target)


@contextmanager
def _staging_tree(target: Path) -> Iterator[Path]:
    """在正式目标同级创建一次性 staging 容器并自动清理。

    staging 与目标处于同一文件系统，使最后的目录重命名不退化为跨卷复制。
    容器还为 :func:`_commit_staged_tree` 提供保存旧目录的 ``previous`` 槽位。
    """

    target.parent.mkdir(parents=True, exist_ok=True)
    container = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=target.parent))
    try:
        yield container / "payload"
    finally:
        # 若 previous 仍存在，说明极端文件系统错误阻止了恢复或旧副本清理。
        # 此时宁可留下带随机名的人工恢复点，也绝不能递归删除用户原有安装。
        recovery = container / "previous"
        if _path_exists(container) and not _path_exists(recovery):
            try:
                _remove_path(container)
            except OSError:
                # 正式目标（或已恢复的旧目标）已不在容器内；清理异常不能反向破坏提交语义。
                shutil.rmtree(container, ignore_errors=True)


def _remove_path(path: Path) -> None:
    """删除受控的单个文件、链接或目录本身，不跟随链接目标。"""

    if not _path_exists(path):
        return
    if _is_link_like(path) or not path.is_dir():
        path.unlink()
    else:
        def make_writable(function: Callable[[str], None], value: str, error: Any) -> None:
            """处理 Windows Git 只读对象：加写权限后重试原删除操作。"""

            del error
            os.chmod(value, stat.S_IWRITE)
            function(value)

        shutil.rmtree(path, onerror=make_writable)


def _commit_staged_tree(staging: Path, target: Path, *, finalize: Callable[[], None] | None = None) -> None:
    """以可回滚目录交换提交已验证 staging，并可绑定一次状态写入。

    若目标已存在，先把它重命名到 staging 容器内的 ``previous``；随后把 payload
    重命名为正式目标，再执行 ``finalize``（例如原子 lock 写入或单语句 SQLite
    upsert）。目录交换或状态写入任一步失败，都会删除新目标并把 ``previous``
    恢复原位，因此调用者不会因一次失败更新丢失可用安装。
    """

    _assert_tree_confined(staging)
    previous = staging.parent / "previous"
    failed = staging.parent / "failed"
    had_target = _path_exists(target)
    installed_new = False
    try:
        if had_target:
            target.replace(previous)
        staging.replace(target)
        installed_new = True
        if finalize is not None:
            finalize()
    except BaseException:
        # 先把失败的新树重命名回随机 staging 容器，再恢复旧树；不能先递归删除新树，
        # 否则 Windows Git 只读对象的清理错误会阻断恢复并让旧目录随容器一起被清理。
        if installed_new and _path_exists(target):
            target.replace(failed)
        if had_target and _path_exists(previous):
            previous.replace(target)
        raise
    else:
        # 状态已成功提交后才删除恢复点。若极端权限问题导致删除失败，外层上下文
        # 会识别仍存在的 previous 并保留它，而不是误删用户旧安装。
        if _path_exists(previous):
            try:
                _remove_path(previous)
            except OSError:
                pass


def _write_json_atomic(path: Path, value: Any) -> None:
    """在同目录完整写入、刷盘并原子替换一个 UTF-8 JSON 文件。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _clone(url: str, target: Path, ref: str | None) -> None:
    """浅克隆 Git 来源，并在 ref 不是 branch/tag 时退化到精确 fetch。

    首先尝试 ``--branch <ref> --depth 1``，这对 tag 和 branch 最高效。
    如果 ref 是只能通过 SHA 定位的提交，则删除失败的临时目标，
    重新 no-checkout clone、浅 fetch 对应 ref，并 detached checkout ``FETCH_HEAD``。
    所有命令都用 argv 执行，URL/ref 不经 shell 重新解析。
    """

    command = ["git", "clone", "--filter=blob:none", "--depth", "1"]
    if ref:
        command += ["--branch", ref]
    command += [url, str(target)]
    try:
        _run(command)
    except RuntimeError:
        if not ref:
            raise
        if target.exists():
            # 只删除由调用者创建的临时/缓存目标，不对任意路径做清理。
            shutil.rmtree(target)
        _run(["git", "clone", "--filter=blob:none", "--no-checkout", url, str(target)])
        _run(["git", "-C", str(target), "fetch", "--depth", "1", "origin", ref])
        _run(["git", "-C", str(target), "checkout", "--detach", "FETCH_HEAD"])


def _find_marketplace_catalog(root: Path) -> Path:
    """按原生优先顺序查找 Marketplace catalog 文件。"""

    for path in (root / ".yy-plugin" / "marketplace.json", root / ".claude-plugin" / "marketplace.json"):
        if path.exists():
            return path
    raise ValueError("marketplace 缺少 marketplace.json")


def _validate_marketplace(path: Path) -> None:
    """校验 Marketplace catalog 的最小结构和每个插件标识。

    根必须是包含 ``plugins`` 列表的 JSON 对象；每个条目必须是对象，
    名称符合 kebab-case 规范且显式提供 ``source``。来源类型的
    更细校验在物化时执行，以便针对不同类型给出准确错误。
    """

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("plugins"), list):
        raise ValueError("marketplace.json 必须包含 plugins 数组")
    for plugin in value["plugins"]:
        if not isinstance(plugin, dict) or not SKILL_NAME.fullmatch(str(plugin.get("name", ""))) or "source" not in plugin:
            raise ValueError("marketplace 含无效插件条目")


def _assert_tree_confined(root: Path) -> None:
    """拒绝安装树中的全部链接、重解析点和真实路径逃逸。

    即便链接仍指向树内，也会让审核时看到的文件边界与执行时解析边界分离，
    并给后续更新制造替换目标的机会，因此安装包采用“零链接”不变量。
    遍历使用 ``os.scandir`` 和 ``follow_symlinks=False``，先检查目录项再决定
    是否递归，不会像 ``rglob``/``copytree`` 默认模式那样先跟随目录链接。
    Windows junction 等重解析点通过 ``st_file_attributes`` 一并拒绝。
    """

    if not _path_exists(root):
        raise ValueError(f"安装包目录不存在：{root}")
    if _is_link_like(root):
        raise ValueError(f"安装包根目录不能是符号链接或重解析点：{root}")
    if not root.is_dir():
        raise ValueError(f"安装包根路径必须是目录：{root}")
    resolved_root = root.resolve(strict=True)
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                metadata = entry.stat(follow_symlinks=False)
                attributes = int(getattr(metadata, "st_file_attributes", 0))
                reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
                if entry.is_symlink() or bool(reparse_flag and attributes & reparse_flag):
                    raise ValueError(f"安装包不允许符号链接或重解析点：{path}")
                try:
                    path.resolve(strict=True).relative_to(resolved_root)
                except ValueError as exc:
                    raise ValueError(f"安装包路径逃逸根目录：{path}") from exc
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(path)


def _tree_hash(root: Path) -> str:
    """计算安装树与遍历顺序无关的 SHA-256 内容指纹。

    每个有效载荷文件的 POSIX 风格相对路径和原始字节都进入摘要，因此内容
    变更、新增/删除和重命名都会改变指纹。``.git`` 元数据不属于插件或 Skill
    有效载荷，且 fresh clone 的 reflog 等文件包含易变时间戳，必须排除，否则
    同一 commit 的重复安装也会错误重置信任。路径分隔符归一化使 Windows 与
    POSIX 系统对同一有效载荷产生相同输入序列。
    """

    # 私有辅助函数也先维护零链接不变量，避免未来新调用方遗漏安装前校验。
    _assert_tree_confined(root)
    digest = hashlib.sha256()
    files = (
        item
        for item in root.rglob("*")
        if item.is_file() and ".git" not in item.relative_to(root).parts
    )
    for path in sorted(files):
        digest.update(str(path.relative_to(root)).replace("\\", "/").encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _git_sha(root: Path) -> str | None:
    """返回 Git 工作树当前 HEAD SHA，非 Git 来源则返回 ``None``。

    SHA 用于追溯来源版本，内容完整性仍以 :func:`_tree_hash` 为准；
    后者能检测未提交修改与非 Git 包。
    """

    try:
        return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    except subprocess.CalledProcessError:
        return None


def _run(argv: list[str]) -> str:
    """以 argv 形式执行安装辅助命令，失败时抛出包含 stderr 的异常。

    函数不启用 shell，所以 URL、ref 和包名中的元字符不会形成额外命令。
    输出按 UTF-8 解码并替换非法字节，使 CLI 在不同平台上都能显示错误。
    """

    result = subprocess.run(argv, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"命令失败：{argv[0]}")
    return result.stdout
