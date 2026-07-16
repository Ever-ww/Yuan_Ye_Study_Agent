"""可审计的生命周期 Hook 引擎；Hook 只能限制或改写，不能授予权限。

原生 Hook 支持 command、HTTP、prompt 和 agent 四类处理器。命令进入 Docker，HTTP
经过公网地址和域名白名单检查，模型型 Hook 由受控回调执行。兼容配置不会自动执行，
插件 Hook 也必须由上层信任过滤后显式传入。
"""

from __future__ import annotations

import asyncio
import json
import shlex
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .sandbox import DockerSandbox
from tools.harness import _SafeRedirectHandler, _enforce_domain_allowlist, _validate_public_url


PromptHook = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class HookOutcome:
    """一组 Hook 执行后的允许状态、受控 payload 和审计消息。"""

    allowed: bool = True
    payload: dict[str, Any] | None = None
    message: str = ""


class HookEngine:
    """按配置顺序串行执行生命周期处理器。"""

    # 使用闭集阻止拼写错误的事件悄悄失效，也限定第三方可挂接的生命周期表面。
    SUPPORTED_EVENTS = {
        "SessionStart", "SessionEnd", "UserPromptSubmit", "BeforeModel", "AfterModel",
        "PreToolUse", "PermissionRequest", "PostToolUse", "ToolFailure", "BeforeCompact",
        "AfterCompact", "MemoryWrite", "SubagentStart", "SubagentStop", "TeammateIdle",
        "TaskCreated", "TaskCompleted", "CronStart", "CronComplete", "Stop",
    }

    def __init__(
        self,
        project_root: Path,
        sandbox: DockerSandbox,
        *,
        prompt_handler: PromptHook | None = None,
        max_depth: int = 3,
        extra_paths: list[Path] | None = None,
        allowed_domains: tuple[str, ...] = (),
    ) -> None:
        """建立 Hook 引擎并立即加载原生及已信任的附加配置。"""

        self.project_root = project_root
        self.sandbox = sandbox
        self.prompt_handler = prompt_handler
        self.max_depth = max_depth
        self.extra_paths = extra_paths or []
        self.allowed_domains = allowed_domains
        self.configs: list[dict[str, Any]] = []
        self.reload()

    def reload(self) -> None:
        """从磁盘重新加载配置，不保留已删除文件的旧处理器。"""

        self.configs = []
        # 兼容配置只可被发现和展示，绝不在这里自动执行。除项目原生文件外，
        # ``extra_paths`` 必须已经由插件信任层逐项筛选。
        for path in (self.project_root / ".yy" / "hooks.json", *self.extra_paths):
            if not path.exists():
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
            hooks = raw.get("hooks", raw) if isinstance(raw, dict) else {}
            if isinstance(hooks, dict):
                self.configs.append({"source": str(path), "hooks": hooks})

    def describe(self) -> list[dict[str, Any]]:
        """返回不含脚本正文和秘密参数的 Hook 配置摘要。"""

        result = []
        for config in self.configs:
            for event, groups in config["hooks"].items():
                result.append({"source": config["source"], "event": event, "groups": len(groups) if isinstance(groups, list) else 0})
        return result

    async def emit(self, event: str, payload: dict[str, Any], *, depth: int = 0) -> HookOutcome:
        """触发事件并依次折叠所有匹配处理器的修改。

        首个拒绝立即短路。后续处理器只看到前序处理器合并后的 payload；任何试图写入
        ``approved`` 或 ``permission`` 的字段都会在合并前删除，保证 Hook 无法提权。
        """

        if event not in self.SUPPORTED_EVENTS:
            raise ValueError(f"未知 Hook 事件：{event}")
        if depth >= self.max_depth:
            return HookOutcome(False, payload, "Hook 递归深度超限")
        current = dict(payload)
        messages: list[str] = []
        for config in self.configs:
            groups = config["hooks"].get(event, [])
            if not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, dict) or not self._matches(group.get("matcher"), current):
                    continue
                handlers = group.get("hooks", [group])
                for handler in handlers:
                    outcome = await self._run_handler(event, current, handler)
                    if outcome.payload:
                        # Hook 可收窄或重写参数，但伪造的审批字段永远不会进入当前状态。
                        outcome.payload.pop("approved", None)
                        outcome.payload.pop("permission", None)
                        current.update(outcome.payload)
                    if outcome.message:
                        messages.append(outcome.message)
                    if not outcome.allowed:
                        return HookOutcome(False, current, "; ".join(messages))
        return HookOutcome(True, current, "; ".join(messages))

    @staticmethod
    def _matches(matcher: Any, payload: dict[str, Any]) -> bool:
        """用 ``|`` 分隔的精确名称列表匹配工具/对象；``*`` 表示全部。"""

        if not matcher or matcher == "*":
            return True
        actual = str(payload.get("tool") or payload.get("name") or "")
        choices = str(matcher).split("|")
        return actual in choices

    async def _run_handler(self, event: str, payload: dict[str, Any], config: dict[str, Any]) -> HookOutcome:
        """执行单个处理器，并统一转换为 ``HookOutcome``。

        超时上限硬截断到 300 秒；命令输出最多接受 100 KB，避免第三方处理器拖垮
        上下文和事件数据库。
        """

        kind = config.get("type", "command")
        timeout = min(300.0, float(config.get("timeout", 30)))
        if kind == "command":
            command = config.get("command")
            argv = command if isinstance(command, list) else shlex.split(str(command), posix=True)
            # 命令型 Hook 不允许静默回退到主机进程；Docker 缺失会安全失败。
            code, stdout, stderr = await self.sandbox.run(argv, cwd=str(self.project_root), timeout=timeout)
            if len(stdout) > 100_000:
                stdout = stdout[:100_000]
            parsed = self._parse_outcome(stdout)
            return HookOutcome(code == 0, parsed, stderr.strip() or (f"Hook exited {code}" if code else ""))
        if kind == "http":
            url = str(config.get("url", ""))
            await asyncio.to_thread(_validate_public_url, url)
            _enforce_domain_allowlist(url, self.allowed_domains)

            def post() -> str:
                """在线程中执行阻塞 urllib 请求，并限制读取长度。"""

                request = urllib.request.Request(
                    url, data=json.dumps({"event": event, "payload": payload}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                opener = urllib.request.build_opener(_SafeRedirectHandler(self.allowed_domains))
                with opener.open(request, timeout=timeout) as response:
                    return response.read(100_001).decode("utf-8", errors="replace")

            output = await asyncio.wait_for(asyncio.to_thread(post), timeout=timeout + 1)
            return HookOutcome(True, self._parse_outcome(output))
        if kind in {"prompt", "agent"}:
            if self.prompt_handler is None:
                return HookOutcome(False, payload, f"{kind} Hook 未配置模型处理器")
            result = await asyncio.wait_for(self.prompt_handler(str(config.get("prompt", "")), payload), timeout=timeout)
            if not isinstance(result, dict):
                # 模型型 Hook 位于安全决策路径上，格式错误必须安全失败，不能尝试把
                # 列表、字符串等宽松转换为可用 payload。
                return HookOutcome(False, payload, f"{kind} Hook 返回值必须是 JSON 对象")

            # 复制回调结果，避免 ``pop`` 修改处理器持有的原始对象，进而让审计记录或
            # 重试看到不同内容。未返回 allow 时沿用兼容语义：仅提供 payload 即允许。
            outcome_payload = dict(result)
            allow = outcome_payload.pop("allow", True)
            message = outcome_payload.pop("message", "")
            if not isinstance(allow, bool):
                # Python 中 ``bool("false")`` 为 True。若在权限边界使用宽松真值转换，
                # 模型最常见的字符串化 JSON 错误会反而放行，因此这里只接受真正 bool。
                return HookOutcome(False, payload, f"{kind} Hook 的 allow 必须是 JSON 布尔值")
            return HookOutcome(allow, outcome_payload, str(message))
        return HookOutcome(False, payload, f"未知 Hook 类型：{kind}")

    @staticmethod
    def _parse_outcome(output: str) -> dict[str, Any]:
        """解析处理器 JSON；普通文本作为附加 Observation 保留。"""

        if not output.strip():
            return {}
        try:
            value = json.loads(output)
        except json.JSONDecodeError:
            return {"observation": output.strip()}
        return value if isinstance(value, dict) else {"observation": str(value)}
