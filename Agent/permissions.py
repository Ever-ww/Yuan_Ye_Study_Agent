"""确定性的能力包匹配、风险分级与人工审批策略。

授权顺序固定为：不可绕过的硬拒绝 → 持久/会话 deny → plan 只读边界 → 后台能力包边界
→ 持久 ask → 持久 allow → 权限模式自动规则 → 人工询问。Hook 无法调用本模块来提升
权限；关键操作即使已有宽泛 allow 也必须逐次确认。
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping
from uuid import uuid4

from .storage import StateStore
from .types import utc_now


class PermissionMode(str, Enum):
    """前台运行时可选择的四种审批模式。"""

    PLAN = "plan"
    REVIEW_ALL = "review-all"
    RISK_BASED = "risk-based"
    ACCEPT_SANDBOXED = "accept-sandboxed"


class ApprovalDecision(str, Enum):
    """一次审批的结果及其允许生效范围。"""

    ALLOW_ONCE = "allow-once"
    ALLOW_SESSION = "allow-session"
    ALLOW_PROJECT = "allow-project"
    ALLOW_USER = "allow-user"
    DENY = "deny"


def plugin_capability_snapshot(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    """把当前启用插件集合规范化为可持久化、可精确比较的能力快照。

    插件 ID 构成集合边界；每个值同时冻结整树内容哈希与已信任可执行组件。信任组件在
    写入前按集合语义排序、去重并重新编码为规范 JSON，避免仅由数组顺序或空白引起误报，
    又能让新增、禁用、内容变化和信任变化中的任一种都改变最终字典。
    """

    snapshot: dict[str, dict[str, str]] = {}
    for row in rows:
        plugin_id = str(row["id"])
        raw_components = row.get("trusted_components_json", "[]")
        try:
            components = json.loads(raw_components) if isinstance(raw_components, str) else raw_components
        except json.JSONDecodeError as exc:
            raise ValueError(f"插件 {plugin_id} 的 trusted_components_json 无效") from exc
        if not isinstance(components, list) or not all(isinstance(item, str) for item in components):
            raise ValueError(f"插件 {plugin_id} 的 trusted_components_json 必须是字符串数组")
        snapshot[plugin_id] = {
            "content_hash": str(row["content_hash"]),
            "trusted_components_json": json.dumps(
                sorted(set(components)), ensure_ascii=False, separators=(",", ":")
            ),
        }
    # 插入顺序不影响字典相等，但稳定顺序让 capability_json 和审计差异保持可复现。
    return {plugin_id: snapshot[plugin_id] for plugin_id in sorted(snapshot)}


@dataclass(frozen=True)
class CapabilityGrant:
    """创建后台任务时冻结的最小能力上限。

    能力包只能缩小权限，不能替代硬拒绝和关键动作确认。空约束元组表示对应维度
    不额外限制，但 ``tools`` 为空仍不会允许任何工具，必须显式列出或使用 ``*``。
    ``plugin_capability_snapshot`` 精确冻结启用插件集合、整树内容哈希和已信任组件；
    ``plugin_versions`` 仅用于读取旧任务，新的 Cron 不再写入该兼容字段。
    """

    tools: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    command_prefixes: tuple[str, ...] = ()
    plugin_capability_snapshot: dict[str, dict[str, str]] | None = None
    plugin_versions: dict[str, str] = field(default_factory=dict)

    def allows(self, tool: str, arguments: dict[str, Any]) -> bool:
        """同时匹配工具、路径、域名和命令前缀，任一维度越界即拒绝。"""

        if "*" not in self.tools and tool not in self.tools:
            return False
        path = arguments.get("path")
        if path and self.paths:
            resolved = str(Path(str(path)).resolve())
            # 仅允许目标等于授权根或位于其下；避免简单字符串前缀把 sibling 误判为子项。
            if not any(resolved == allowed or resolved.startswith(allowed + str(Path("/"))) for allowed in self.paths):
                return False
        domain_value = arguments.get("domain") or arguments.get("url")
        if domain_value and self.domains:
            hostname = urllib.parse.urlparse(str(domain_value)).hostname or str(domain_value)
            # 点边界后缀匹配允许合法子域，不允许 ``evil-example.com`` 冒充 ``example.com``。
            if not any(hostname == allowed or hostname.endswith("." + allowed) for allowed in self.domains):
                return False
        command = arguments.get("command") or (" ".join(str(value) for value in arguments.get("argv", [])) if arguments.get("argv") else None)
        if command and self.command_prefixes and not any(str(command).startswith(prefix) for prefix in self.command_prefixes):
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        """导出可写入 Cron ``capability_json`` 的普通字典。"""

        return asdict(self)


@dataclass(frozen=True)
class PermissionRequest:
    """呈现给 CLI/Web 审批界面的完整调用上下文。"""

    tool: str
    arguments: dict[str, Any]
    risk: str
    sandboxed: bool
    reason: str


ApprovalCallback = Callable[[PermissionRequest], Awaitable[ApprovalDecision]]


class PermissionBroker:
    """对单次工具调用给出可审计的允许/拒绝结论。"""

    # 这些名称代表权限系统本身或灾难性根操作，即使配置了 allow 也永远拒绝。
    HARD_DENIED_NAMES = {"credential_export", "disable_sandbox", "delete_root"}
    # ``plan`` 和 risk-based 自动批准仅覆盖无副作用的读取、检索及本地计算工具。
    PASSIVE_TOOLS = {
        "read_file", "list_files", "search_text", "calculator", "current_time",
        "git_status", "git_diff", "git_log", "memory_search", "corpus_search",
        "lsp_hover", "lsp_definition", "lsp_references", "lsp_diagnostics",
        # task_create/task_update 会写入 SQLite，不属于 plan/risk-based 可自动放行的被动读取。
        "ask_user", "task_list",
    }

    def __init__(
        self,
        store: StateStore,
        project_root: Path,
        mode: PermissionMode | str = PermissionMode.RISK_BASED,
        approval_callback: ApprovalCallback | None = None,
    ) -> None:
        """绑定状态库、项目边界、审批模式及可选交互回调。"""

        self.store = store
        self.project_root = project_root.resolve()
        self.mode = PermissionMode(mode)
        self.approval_callback = approval_callback
        self._session_rules: list[tuple[str, dict[str, Any]]] = []

    async def authorize(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        risk: str,
        sandboxed: bool,
        grant: CapabilityGrant | None = None,
    ) -> tuple[bool, str]:
        """按固定优先级授权一次调用，并返回结论与可显示原因。

        ``grant`` 用于后台 Cron/任务，存在时调用必须完全落在能力包内，且不再请求
        扩权；这样无人值守任务只能暂停或失败，不能自己扩大权限范围。
        """

        if tool in self.HARD_DENIED_NAMES:
            return False, "命中不可绕过的硬拒绝规则"
        critical = self._critical_call(tool, arguments, risk)
        matched = self._match_rule(tool, arguments)
        if matched == "deny":
            return False, "命中持久权限规则：deny"
        # plan 是不可被历史授权、人工询问或后台能力包扩大的只读上限。被动工具仍继续经过
        # ask 和 grant 检查，使更严格的显式规则与后台最小能力边界保持有效。
        if self.mode == PermissionMode.PLAN and tool not in self.PASSIVE_TOOLS:
            return False, "plan 模式仅允许只读工具"
        if grant is not None:
            grant_arguments = dict(arguments)
            # 能力包保存绝对边界；相对工具路径先以项目根解析后再参与匹配。
            if grant_arguments.get("path"):
                candidate = Path(str(grant_arguments["path"]))
                grant_arguments["path"] = str((candidate if candidate.is_absolute() else self.project_root / candidate).resolve())
            if not grant.allows(tool, grant_arguments):
                return False, "后台能力包不包含该调用"
            if critical:
                return False, "后台能力包不能授权关键主机或桌面操作"
            # ``ask`` 是管理员显式设置的逐次审批策略。即使任务已经持有能力包，也只能
            # 证明调用没有越过任务创建时冻结的上限，不能据此绕过更严格的询问规则。
            if matched == "ask":
                return await self._ask(
                    PermissionRequest(tool, arguments, risk, sandboxed, self._reason(tool, risk, sandboxed))
                )
            return True, "创建任务时批准的后台能力包"
        # 显式 ``ask`` 的优先级高于持久 allow、权限模式自动规则和沙箱自动放行。
        # 若当前前端没有审批回调，``_ask`` 会安全拒绝，而不是退回到宽松模式。
        if matched == "ask":
            effective_risk = "critical" if critical else risk
            return await self._ask(
                PermissionRequest(
                    tool,
                    arguments,
                    effective_risk,
                    sandboxed,
                    self._reason(tool, effective_risk, sandboxed),
                )
            )
        # 关键调用不可依靠长期 allow 跳过最终确认。
        if matched == "allow" and not critical:
            return True, "命中持久权限规则：allow"
        if self.mode == PermissionMode.PLAN:
            return True, "plan 模式仅允许只读工具"
        if self.mode == PermissionMode.RISK_BASED and tool in self.PASSIVE_TOOLS and risk == "low":
            return True, "低风险只读工具"
        if self.mode == PermissionMode.ACCEPT_SANDBOXED and sandboxed and not critical:
            return True, "accept-sandboxed 自动允许沙箱内调用"
        effective_risk = "critical" if critical else risk
        return await self._ask(PermissionRequest(tool, arguments, effective_risk, sandboxed, self._reason(tool, effective_risk, sandboxed)))

    def _match_rule(self, tool: str, arguments: dict[str, Any]) -> str | None:
        """按 deny → ask → allow 的顺序匹配持久规则，再处理会话 allow。

        会话 deny 先于数据库规则单独检查，确保任何更宽的持久 allow 都不能覆盖它。
        当前公开审批只产生 allow，但保留 deny/ask 顺序以支持管理界面写入策略规则。
        """

        candidates = self.store.query(
            "SELECT effect,specifier_json FROM permission_rules WHERE tool=? AND (project_root IS NULL OR project_root=?) ORDER BY CASE effect WHEN 'deny' THEN 0 WHEN 'ask' THEN 1 ELSE 2 END, created_at",
            (tool, str(self.project_root)),
        )
        for effect, specifier in self._session_rules:
            if tool == specifier.get("tool") and self._specifier_matches(specifier.get("arguments", {}), arguments):
                if effect == "deny":
                    return effect
        for row in candidates:
            specifier = json.loads(row["specifier_json"])
            if self._specifier_matches(specifier, arguments):
                return str(row["effect"])
        for effect, specifier in self._session_rules:
            if tool == specifier.get("tool") and self._specifier_matches(specifier.get("arguments", {}), arguments):
                return effect
        return None

    def inherit_session_rules(self, parent: PermissionBroker) -> None:
        """复制父运行时的会话规则，使子代理不能绕过当前会话边界。

        规则参数只包含 JSON 兼容标量和容器；通过 JSON 往返深复制，避免父子 Broker
        后续修改同一个嵌套对象。项目/用户规则仍由双方从共享 SQLite 独立查询。
        """

        self._session_rules = [
            (effect, json.loads(json.dumps(specifier, ensure_ascii=False)))
            for effect, specifier in parent._session_rules
        ]

    @staticmethod
    def _specifier_matches(rule: dict[str, Any], actual: dict[str, Any]) -> bool:
        """执行确定性的精确键值子集匹配，不解释通配符或正则表达式。"""

        return all(actual.get(key) == value for key, value in rule.items())

    async def _ask(self, request: PermissionRequest) -> tuple[bool, str]:
        """调用前端审批，并按选择保存会话、项目或用户级 allow。"""

        if self.approval_callback is None:
            return False, "需要人工审批，但当前运行环境无法询问"
        decision = await self.approval_callback(request)
        # “始终允许”对关键动作自动降级为仅本次允许，防止永久跳过最终确认。
        if request.risk == "critical" and decision not in {ApprovalDecision.ALLOW_ONCE, ApprovalDecision.DENY}:
            decision = ApprovalDecision.ALLOW_ONCE
        if decision == ApprovalDecision.DENY:
            return False, "用户拒绝"
        if decision == ApprovalDecision.ALLOW_SESSION:
            self._session_rules.append(("allow", {"tool": request.tool, "arguments": request.arguments}))
        elif decision in {ApprovalDecision.ALLOW_PROJECT, ApprovalDecision.ALLOW_USER}:
            scope = "project" if decision == ApprovalDecision.ALLOW_PROJECT else "user"
            self.store.execute(
                "INSERT INTO permission_rules VALUES(?,?,?,?,?,?,?)",
                (
                    uuid4().hex, "allow", scope,
                    str(self.project_root) if scope == "project" else None,
                    request.tool, json.dumps(request.arguments, ensure_ascii=False, sort_keys=True), utc_now(),
                ),
            )
        return True, f"用户批准：{decision.value}"

    @staticmethod
    def _reason(tool: str, risk: str, sandboxed: bool) -> str:
        """生成供人类判断风险的简短、稳定说明。"""

        boundary = "沙箱内" if sandboxed else "沙箱外"
        return f"{tool} 被评估为 {risk} 风险并将在{boundary}执行"

    @staticmethod
    def _critical_call(tool: str, arguments: dict[str, Any], risk: str) -> bool:
        """识别永远需要逐次批准的桌面与可写主机级操作。"""

        if risk == "critical" or tool == "desktop":
            return True
        if tool == "shell" and bool(arguments.get("writable")):
            return True
        return False
