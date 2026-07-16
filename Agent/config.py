"""分层 ``.yy`` 配置加载、运行目录推导和旧 ``config.ini`` 迁移。

配置合并顺序从低到高为默认值、用户配置、项目共享配置、项目本机配置、调用方覆盖。
密钥不属于普通 JSON 配置：旧配置迁移时只写入系统凭据存储，绝不写入可提交文件。
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
from configparser import ConfigParser
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SandboxConfig:
    """Docker 沙箱及网络/文件读取边界的声明式配置。"""

    enabled: bool = True
    fail_if_unavailable: bool = True
    docker_image: str = "python:3.12-slim"
    allow_unsandboxed_commands: bool = False
    allowed_domains: list[str] = field(default_factory=list)
    deny_read: list[str] = field(default_factory=lambda: ["~/.ssh", "~/.aws", "~/.config/gcloud"])


@dataclass
class RuntimeConfig:
    """解析完成、可直接注入 ``AgentRuntime`` 的完整配置。

    ``extras`` 保留当前版本尚不识别的配置键，以便前向兼容插件或后续版本字段。
    路径属性按需计算，不在配置文件中持久化机器相关绝对路径。
    """

    project_root: Path
    profile: str = "general"
    permission_mode: str = "risk-based"
    model: str | None = None
    fallback_model: str | None = None
    max_steps: int = 20
    temperature: float = 0.0
    context_event_limit: int = 80
    auto_memory: bool = True
    max_team_agents: int = 4
    web_search_url: str | None = None
    vision_model: str | None = None
    timeout_seconds: float = 60.0
    providers: dict[str, dict[str, str]] = field(default_factory=dict)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验直接 Python 构造和文件加载共享的核心不变量。"""

        if self.profile not in {"general", "study", "code"}:
            raise ValueError("profile 必须是 general、study 或 code")
        if self.max_steps < 1:
            raise ValueError("max_steps 至少为 1")
        # 压缩结果会保留系统提示、摘要和最近 20 条消息。更小的限制无法真正缩短上下文，
        # 反而会让每个 turn 重复触发摘要，因此直接 API 与配置文件都拒绝该值。
        if self.context_event_limit < 22:
            raise ValueError("context_event_limit 至少为 22")
        if self.web_search_url is not None:
            endpoint = str(self.web_search_url)
            if "{query}" not in endpoint:
                raise ValueError("web_search_url 必须包含 {query} 占位符")
            try:
                rendered = endpoint.format(query="test")
            except (KeyError, ValueError) as exc:
                raise ValueError("web_search_url 含不支持的模板占位符") from exc
            parsed = urllib.parse.urlparse(rendered)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("web_search_url 必须是 http/https URL 模板")

    @property
    def yy_dir(self) -> Path:
        """返回项目内可共享 Harness 配置目录。"""

        return self.project_root / ".yy"

    @property
    def user_dir(self) -> Path:
        """返回本机用户状态根目录，允许 ``YY_AGENT_HOME`` 显式覆盖。"""

        return Path(os.getenv("YY_AGENT_HOME", Path.home() / ".yy")).expanduser().resolve()

    @property
    def project_id(self) -> str:
        """由正规化仓库绝对路径计算稳定且不泄露原路径的短标识。"""

        normalized = os.path.normcase(str(self.project_root.resolve()))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    @property
    def state_dir(self) -> Path:
        """返回该项目在用户目录下的私有运行状态目录。"""

        return self.user_dir / "projects" / self.project_id

    @property
    def state_db(self) -> Path:
        """返回事件、权限、记忆和调度共享的 SQLite 文件路径。"""

        return self.state_dir / "state.db"

    def to_dict(self) -> dict[str, Any]:
        """导出 JSON 友好配置，将唯一的 ``Path`` 字段转为字符串。"""

        data = asdict(self)
        data["project_root"] = str(self.project_root)
        return data


# 默认值保持为普通字典，便于在任何文件读取前参与相同的深度合并流程。
DEFAULTS: dict[str, Any] = {
    "profile": "general",
    "permission_mode": "risk-based",
    "max_steps": 20,
    "temperature": 0.0,
    "context_event_limit": 80,
    "auto_memory": True,
    "max_team_agents": 4,
    "sandbox": asdict(SandboxConfig()),
}


def _load_json(path: Path) -> dict[str, Any]:
    """读取对象型 JSON；文件不存在表示该配置层为空。"""

    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"配置必须是 JSON 对象：{path}")
    return raw


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并映射，非映射值由更高优先级配置整体替换。"""

    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def find_project_root(start: str | Path | None = None) -> Path:
    """向上寻找最近的 ``.git`` 或 ``.yy``，找不到则使用起点。"""

    current = Path(start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists() or (candidate / ".yy").exists():
            return candidate
    return current


def load_runtime_config(
    project_root: str | Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
) -> RuntimeConfig:
    """按稳定优先级加载并校验运行时配置。

    ``overrides`` 通常来自 CLI，因此最后合并。这里只做核心字段的类型正规化和必要
    约束校验；未知字段进入 ``extras``，不会静默丢失。
    """

    root = find_project_root(project_root)
    home = Path(os.getenv("YY_AGENT_HOME", Path.home() / ".yy")).expanduser()
    data = dict(DEFAULTS)
    # 后读取的层覆盖先读取的层；项目本机配置高于可提交的项目共享配置。
    for path in (home / "settings.json", root / ".yy" / "settings.json", root / ".yy" / "settings.local.json"):
        data = _merge(data, _load_json(path))
    data = _merge(data, overrides or {})
    known = {
        "profile", "permission_mode", "model", "fallback_model", "max_steps", "temperature",
        "context_event_limit", "auto_memory", "max_team_agents", "web_search_url", "vision_model", "timeout_seconds", "providers", "sandbox",
    }
    sandbox = SandboxConfig(**data.get("sandbox", {}))
    extras = {key: value for key, value in data.items() if key not in known}
    config = RuntimeConfig(
        project_root=root,
        profile=str(data.get("profile", "general")),
        permission_mode=str(data.get("permission_mode", "risk-based")),
        model=data.get("model"),
        fallback_model=data.get("fallback_model"),
        max_steps=int(data.get("max_steps", 20)),
        temperature=float(data.get("temperature", 0)),
        context_event_limit=int(data.get("context_event_limit", 80)),
        auto_memory=bool(data.get("auto_memory", True)),
        max_team_agents=int(data.get("max_team_agents", 4)),
        web_search_url=data.get("web_search_url"),
        vision_model=data.get("vision_model"),
        timeout_seconds=float(data.get("timeout_seconds", 60)),
        providers={str(name): {str(key): str(value) for key, value in values.items()} for name, values in (data.get("providers") or {}).items() if isinstance(values, dict)},
        sandbox=sandbox,
        extras=extras,
    )
    return config


def migrate_legacy_config(root: Path, *, overwrite: bool = False) -> Path:
    """将旧 INI 模型设置迁移为本机 JSON，并安全迁移 API Key。

    当发现密钥但系统未安装 ``keyring`` 时会在写文件前失败，避免调用者误以为密钥
    已安全保存。目标存在时默认拒绝覆盖，防止破坏手工调整后的本机配置。
    """

    source = root / "config.ini"
    target = root / ".yy" / "settings.local.json"
    if target.exists() and not overwrite:
        raise FileExistsError(f"目标配置已存在：{target}")
    parser = ConfigParser()
    if source.exists():
        parser.read(source, encoding="utf-8")
    model = parser["model_choice"] if parser.has_section("model_choice") else {}
    providers: dict[str, dict[str, str]] = {}
    secrets_to_migrate: dict[str, str] = {}
    for section in parser.sections():
        if section.startswith("providers."):
            provider_name = section.removeprefix("providers.")
            values = {key: value for key, value in parser[section].items() if key != "api_key" and value}
            if values:
                providers[provider_name] = values
            api_key = parser[section].get("api_key", "").strip()
            if api_key:
                secrets_to_migrate[provider_name] = api_key
    if secrets_to_migrate:
        # 普通配置文件只保留非秘密 Provider 元数据，密钥写入操作系统凭据后端。
        try:
            import keyring
        except ImportError as exc:
            raise RuntimeError("旧配置包含 API Key。请先安装 yy-agent[keyring]，或将密钥移到环境变量后再迁移。") from exc
        for provider_name, api_key in secrets_to_migrate.items():
            keyring.set_password("yy-agent", provider_name, api_key)
    output = {
        "model": model.get("default_model", "deepseek"),
        "fallback_model": model.get("fallback_model", "") or None,
        "providers": providers,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target
