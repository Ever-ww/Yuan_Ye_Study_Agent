"""异步 Harness 的内置工具、工作区边界与文件变更留痕。

所有文件路径都必须经过 :class:`PathPolicy` 的真实路径校验；
写操作采用同目录临时文件和原子替换，并把前后快照交给
``StateStore``，以支持只回滚 Agent 自身变更的 ``rewind``。Shell 与
浏览器等执行能力始终经由 Runtime 审批，高风险进程默认交给
Docker 隔离层。网络工具另外实施域名白名单、SSRF 和响应大小限制。
"""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import ipaddress
import json
import operator
import os
import re
import socket
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from Agent.sandbox import DockerSandbox
from Agent.storage import StateStore
from Agent.types import AsyncTool, ImageContent, ToolResult

if TYPE_CHECKING:
    from Agent.permissions import CapabilityGrant


@dataclass
class ToolContext:
    """一次工具执行所需的受控运行时上下文。

    ``project_root`` 是所有工作区路径的信任根；``store`` 负责事件与
    变更留痕；``sandbox`` 承担高风险命令隔离。``allowed_domains`` 是
    当前会话已批准的网络能力上限，工具不得自行扩展；``capability_grant``
    继续传给可能创建子执行上下文的工具，避免扩展绕过后台任务上限。
    """

    session_id: str
    project_root: Path
    store: StateStore
    sandbox: DockerSandbox
    allowed_domains: tuple[str, ...] = ()
    question_callback: Callable[[str, list[str]], Awaitable[str]] | None = None
    capability_grant: CapabilityGrant | None = None


class PathPolicy:
    """将模型提供的路径约束在项目真实路径内。

    单纯检查 ``..`` 不足以防止逃逸，因为工作区内的符号链接可以
    指向外部。本策略先解析真实路径，再用 ``relative_to`` 验证它仍在
    根目录下。对尚未存在的写入目标，则先解析已存在的父目录，
    避免 ``Path.resolve`` 对缺失末端的差异影响安全边界。
    """

    SENSITIVE_NAMES = {".env", "id_rsa", "id_ed25519", "credentials", "secrets.json"}

    def __init__(self, root: Path) -> None:
        """固定并解析工作区信任根。"""

        self.root = root.resolve()

    def resolve(self, value: str, *, for_write: bool = False) -> Path:
        """返回工作区内的真实路径，越界或敏感目标将被拒绝。

        Args:
            value: 用户或模型提供的绝对/相对路径。
            for_write: 目标可以尚未存在时设为 ``True``。
        """

        raw = Path(value)
        candidate = raw if raw.is_absolute() else self.root / raw
        if for_write and not candidate.exists():
            # 新文件无可解析 inode，因此用父目录的真实路径重建目标。
            parent = candidate.parent.resolve()
            resolved = parent / candidate.name
        else:
            resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise PermissionError(f"路径超出工作区：{value}") from exc
        # 任何一层路径命中敏感名称都拒绝，不只检查最终文件名。
        if any(part.lower() in self.SENSITIVE_NAMES for part in resolved.parts):
            raise PermissionError(f"默认拒绝访问敏感文件：{resolved.name}")
        return resolved


@dataclass
class BaseTool:
    """异步工具的通用元数据与执行接口。

    ``risk`` 供权限引擎决定是否需要审批，``sandboxed`` 表示工具
    是否能在隔离环境中执行。这两个字段都是声明，真正的授权仍由
    Runtime 在每次调用前判定。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    risk: str = "low"
    sandboxed: bool = False

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """执行工具；具体子类必须返回结构化 :class:`ToolResult`。"""

        raise NotImplementedError


class ToolRegistry:
    """管理异步工具实例、Schema 与调用前参数校验。

    工具名在单个运行时中必须唯一，否则后注册工具可能悄然覆盖
    已审批工具。Schema 校验优先使用 ``jsonschema``；可选依赖缺失时
    仍会检查必填字段，让核心 CLI 可用，但完整类型约束需要安装依赖。
    """

    def __init__(self, tools: list[AsyncTool] | None = None) -> None:
        """创建注册表并按顺序注册初始工具。"""

        self._tools: dict[str, AsyncTool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: AsyncTool) -> None:
        """注册一个名称唯一的工具，拒绝隐式覆盖。"""

        if tool.name in self._tools:
            raise ValueError(f"工具重复注册：{tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> AsyncTool | None:
        """按稳定名称查找工具，未注册时返回 ``None``。"""

        return self._tools.get(name)

    def schemas(self, allowed: set[str] | None = None) -> list[dict[str, Any]]:
        """生成供模型 tool-calling 使用的 Schema 列表。

        ``allowed`` 是当前会话的能力上限；在组合 Prompt 前过滤可减少
        模型尝试调用不可用工具的机会，但执行时仍需要再次鉴权。
        """

        return [
            {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
            for tool in self._tools.values() if allowed is None or tool.name in allowed
        ]

    def names(self) -> list[str]:
        """按注册顺序返回所有工具名。"""

        return list(self._tools)

    def validate(self, name: str, arguments: dict[str, Any]) -> None:
        """在工具执行前校验参数是否符合声明的 JSON Schema。"""

        tool = self._tools.get(name)
        if not tool:
            raise KeyError(name)
        try:
            import jsonschema
        except ImportError:
            # 无可选依赖时保留最小防线；doctor 会提示安装完整校验器。
            required = tool.parameters.get("required", [])
            missing = [key for key in required if key not in arguments]
            if missing:
                raise ValueError(f"缺少必填参数：{', '.join(missing)}")
            return
        jsonschema.validate(arguments, tool.parameters)


@dataclass
class ReadFileTool(BaseTool):
    """分段读取工作区内的 UTF-8 文本文件。

    2MB 总大小和 2000 行单次输出上限用于保护上下文窗口；
    行号随文本一起返回，便于 Agent 后续精确定位和解释修改。
    """

    name: str = "read_file"
    description: str = "Read a UTF-8 text file inside the workspace."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["path"]})

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """校验路径与大小，返回指定行窗口及文件元数据。"""

        path = PathPolicy(context.project_root).resolve(str(arguments["path"]))
        if path.stat().st_size > 2_000_000:
            raise ValueError("文件超过 2MB，请分块读取")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        offset = max(0, int(arguments.get("offset", 0)))
        limit = min(2000, max(1, int(arguments.get("limit", 400))))
        content = "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines[offset:offset + limit], start=offset))
        return ToolResult("", self.name, content, metadata={"path": str(path), "total_lines": len(lines)})


@dataclass
class ReadImageTool(BaseTool):
    """从工作区读取受支持的图片并转换为模型图像内容。

    格式由文件扩展名白名单决定，同时设置 10MB 上限，防止
    Base64 膨胀导致会话事件和模型请求过大。
    """

    name: str = "read_image"
    description: str = "Load a PNG, JPEG, GIF, or WebP image from the workspace for a vision-capable model."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """校验图片路径、格式和大小，返回 Base64 编码的 ``ImageContent``。"""

        path = PathPolicy(context.project_root).resolve(str(arguments["path"]))
        media = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}.get(path.suffix.lower())
        if not media:
            raise ValueError("不支持的图片格式")
        data = path.read_bytes()
        if len(data) > 10_000_000:
            raise ValueError("图片超过 10MB")
        image = ImageContent(media, base64.b64encode(data).decode("ascii"))
        return ToolResult("", self.name, f"Image loaded: {path.relative_to(context.project_root)}", metadata={"path": str(path)}, images=(image,))


@dataclass
class ListFilesTool(BaseTool):
    """递归列出工作区目录下的文件和子目录。

    默认忽略 Git 内部数据与 Python 缓存，并对结果数设上限，
    以避免在大型仓库中生成无界的 Prompt 输入。
    """

    name: str = "list_files"
    description: str = "List files below a workspace directory."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"path": {"type": "string"}, "max_results": {"type": "integer"}}})

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """在已校验目录下遍历条目，返回项目根相对路径。"""

        root = PathPolicy(context.project_root).resolve(str(arguments.get("path", ".")))
        maximum = min(5000, max(1, int(arguments.get("max_results", 500))))
        entries = []
        for path in root.rglob("*"):
            if ".git" in path.parts or "__pycache__" in path.parts:
                continue
            entries.append(str(path.relative_to(context.project_root)))
            if len(entries) >= maximum:
                break
        return ToolResult("", self.name, "\n".join(entries), metadata={"count": len(entries)})


@dataclass
class SearchTextTool(BaseTool):
    """通过 ripgrep 在工作区内执行只读文本检索。

    命令使用 argv 数组而非 shell 字符串，并在查询前加 ``--``，
    因此以连字符开头的搜索词不会被当作 rg 选项解析。
    """

    name: str = "search_text"
    description: str = "Search workspace text with ripgrep."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"}}, "required": ["query"]})

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """执行受路径约束的 rg 搜索，并截断过长输出。"""

        path = PathPolicy(context.project_root).resolve(str(arguments.get("path", ".")))
        argv = ["rg", "-n", "--no-heading", "--color", "never"]
        if arguments.get("glob"):
            argv += ["-g", str(arguments["glob"])]
        # ``--`` 终止选项解析，是防止用户查询变成命令参数的关键边界。
        argv += ["--", str(arguments["query"]), str(path)]
        process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        output = stdout.decode("utf-8", errors="replace")[:200_000]
        # rg 的退出码 1 代表“无匹配”而非执行故障。
        if process.returncode not in (0, 1):
            return ToolResult("", self.name, stderr.decode("utf-8", errors="replace"), True)
        return ToolResult("", self.name, output or "No matches")


@dataclass
class WriteFileTool(BaseTool):
    """原子写入工作区文件，并记录可安全回滚的变更快照。

    内容先写入目标同目录的临时文件，``flush`` 和 ``fsync`` 确保
    数据交给操作系统，最后用 ``os.replace`` 原子替换；这样即使进程
    中断，也不会把半个文件留给用户。前后字节快照交给存储层，
    供 rewind 通过哈希检测用户同期修改，而不使用破坏性 Git 回滚。
    """

    name: str = "write_file"
    description: str = "Atomically write a UTF-8 file inside the workspace."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]})
    risk: str = "medium"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """在工作区内原子写入 UTF-8 内容，并返回变更 ID 和内容哈希。"""

        path = PathPolicy(context.project_root).resolve(str(arguments["path"]), for_write=True)
        content = str(arguments["content"])
        if len(content.encode("utf-8")) > 5_000_000:
            raise ValueError("单次写入不能超过 5MB")
        # ``None`` 明确表示写入前文件不存在，rewind 时应删除而非恢复空文件。
        before = path.read_bytes() if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            # 临时文件与目标同目录，避免跨文件系统时 replace 失去原子性。
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        after = path.read_bytes()
        # 留存原始字节而非文本 diff，可精确恢复换行符和编码内容。
        change_id = context.store.record_file_change(context.session_id, str(path), before, after)
        return ToolResult("", self.name, f"已写入 {path.relative_to(context.project_root)}", metadata={"change_id": change_id, "sha256": hashlib.sha256(after).hexdigest()})


@dataclass
class ApplyPatchTool(BaseTool):
    """对文件中唯一的精确文本片段执行替换。

    ``old`` 必须恰好出现一次，这个不变量可防止含糊补丁在文件
    变化后修改错误位置。实际写入复用 :class:`WriteFileTool`，因此与
    完整写入共享原子性和变更日志语义。
    """

    name: str = "apply_patch"
    description: str = "Replace one exact text occurrence inside a workspace file."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}, "required": ["path", "old", "new"]})
    risk: str = "medium"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """验证唯一匹配后替换文本，并透传原子写入的变更元数据。"""

        path = PathPolicy(context.project_root).resolve(str(arguments["path"]))
        before = path.read_bytes()
        text = before.decode("utf-8")
        old, new = str(arguments["old"]), str(arguments["new"])
        count = text.count(old)
        if count != 1:
            raise ValueError(f"old 文本必须恰好出现一次，实际出现 {count} 次")
        updated = text.replace(old, new, 1)
        result = await WriteFileTool().run({"path": str(path.relative_to(context.project_root)), "content": updated}, context)
        return ToolResult("", self.name, f"已更新 {path.relative_to(context.project_root)}", metadata=result.metadata)


@dataclass
class ShellTool(BaseTool):
    """在 Docker 沙箱中以 argv 形式执行命令。

    工具不调用宿主 shell，因此 ``;``、``|``、``$()`` 等元字符只是
    普通参数，不会被二次解析。工作区默认只读、网络默认关闭；
    ``writable`` 和 ``network`` 是需要经过权限引擎的显式能力，
    而不是模型可信任的自我授权。Docker 不可用时由沙箱层安全失败。
    """

    name: str = "shell"
    description: str = "Run an argv command in the Docker sandbox; shell metacharacters are not interpreted."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"argv": {"type": "array", "items": {"type": "string"}}, "writable": {"type": "boolean"}, "network": {"type": "boolean"}, "timeout": {"type": "number"}}, "required": ["argv"]})
    risk: str = "high"
    sandboxed: bool = True

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """校验 argv，将权限标志和有界超时交给 Docker 沙箱执行。"""

        argv = arguments.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
            raise ValueError("argv 必须是非空字符串数组")
        # 上限防止模型通过参数创建无限期占用资源的容器进程。
        code, stdout, stderr = await context.sandbox.run(
            argv, cwd=str(context.project_root), writable=bool(arguments.get("writable", False)),
            network=bool(arguments.get("network", False)), timeout=min(1800, float(arguments.get("timeout", 120))),
        )
        content = (stdout + ("\n[stderr]\n" + stderr if stderr else ""))[:300_000]
        return ToolResult("", self.name, content, code != 0, {"exit_code": code})


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}


def _calculate(node: ast.AST) -> int | float:
    """按白名单递归计算异步计算器的 AST 节点。

    与旧同步工具一样，这里不使用 ``eval``，并显式排除布尔常量。
    任何函数调用、变量名、容器或属性访问都会落入拒绝分支。
    """

    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        right = _calculate(node.right)
        # 巨大指数可造成 CPU/内存拒绝服务，在真正执行运算前拦截。
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("指数过大")
        return _OPS[type(node.op)](_calculate(node.left), right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_calculate(node.operand))
    raise ValueError("只支持基础算术")


@dataclass
class CalculatorTool(BaseTool):
    """异步 Harness 的受限基础算术工具。

    表达式长度和 AST 节点类型均受限，适合代替模型的不稳定
    心算，但不用作通用 Python 解释器。
    """

    name: str = "calculator"
    description: str = "Safely evaluate a basic arithmetic expression."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]})

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """计算 ``expression`` 并返回数值的字符串表示。"""

        del context
        expression = str(arguments["expression"])
        if len(expression) > 200:
            raise ValueError("表达式过长")
        return ToolResult("", self.name, str(_calculate(ast.parse(expression, mode="eval").body)))


@dataclass
class CurrentTimeTool(BaseTool):
    """返回指定 IANA 时区的当前时间。

    结果使用秒精度 ISO 8601 并包含 UTC 偏移，可直接用于事件、
    计划和人类可读响应。
    """

    name: str = "current_time"
    description: str = "Get current time in an IANA timezone."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"timezone": {"type": "string"}}})

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """解析时区名并返回当前时间，无效时区异常由上层处理。"""

        del context
        return ToolResult("", self.name, datetime.now(ZoneInfo(str(arguments.get("timezone", "Asia/Shanghai")))).isoformat(timespec="seconds"))


@dataclass
class AskUserTool(BaseTool):
    """当关键信息缺失时，通过前端回调询问用户。

    回调由 CLI 或 Web 界面注入，使核心工具不依赖任何具体 UI。
    无交互能力的后台运行会明确失败，不会猜测用户回答。
    """

    name: str = "ask_user"
    description: str = "Ask the user one blocking question when essential information is missing."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"question": {"type": "string"}, "choices": {"type": "array", "items": {"type": "string"}}}, "required": ["question"]})

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """把问题和可选项交给当前 UI 回调，并返回用户原始回答。"""

        if context.question_callback is None:
            return ToolResult("", self.name, "当前运行环境无法询问用户", True)
        answer = await context.question_callback(str(arguments["question"]), [str(value) for value in arguments.get("choices", [])])
        return ToolResult("", self.name, answer)


@dataclass
class TaskCreateTool(BaseTool):
    """为当前会话创建带依赖关系的轻量任务。

    依赖 ID 必须已存在于同一会话，避免产生无法满足的悬空边
    或跨会话污染。该任务表用于当前 Run 的可视化计划，与持久
    Cron 和 Team DAG 是不同概念。
    """

    name: str = "task_create"
    description: str = "Create a session task with optional dependency IDs."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"title": {"type": "string"}, "dependencies": {"type": "array", "items": {"type": "string"}}}, "required": ["title"]})

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """校验同会话依赖后创建 ``pending`` 任务。"""

        dependencies = [str(value) for value in arguments.get("dependencies", [])]
        # 按 session_id 限定查询，防止不同会话恰好出现相同短 ID 时错连依赖。
        existing = {row["id"] for row in context.store.query("SELECT id FROM run_tasks WHERE session_id=?", (context.session_id,))}
        missing = set(dependencies) - existing
        if missing:
            raise ValueError(f"未知依赖任务：{', '.join(sorted(missing))}")
        task_id, now = uuid4().hex[:8], datetime.now().astimezone().isoformat()
        context.store.execute("INSERT INTO run_tasks VALUES(?,?,?,?,?,?,?)", (task_id, context.session_id, str(arguments["title"]), "pending", json.dumps(dependencies), now, now))
        return ToolResult("", self.name, f"已创建任务 {task_id}")


@dataclass
class TaskListTool(BaseTool):
    """按创建时间列出当前会话的轻量任务计划。"""

    name: str = "task_list"
    description: str = "List the current session's task plan."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """以 JSON 形式返回当前 session 的全部任务记录。"""

        del arguments
        rows = context.store.query("SELECT * FROM run_tasks WHERE session_id=? ORDER BY created_at", (context.session_id,))
        return ToolResult("", self.name, json.dumps(rows, ensure_ascii=False, indent=2))


@dataclass
class TaskUpdateTool(BaseTool):
    """更新当前会话任务的受限状态。

    可选状态由 JSON Schema 限定为 ``pending``、``in_progress`` 和
    ``completed``，且 SQL 查找同时匹配任务 ID 和 session ID，避免
    Agent 误更新其他会话的计划。
    """

    name: str = "task_update"
    description: str = "Update a session task status."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["id", "status"]})

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """确认任务归属当前会话后更新状态与时间戳。"""

        rows = context.store.query("SELECT id FROM run_tasks WHERE id=? AND session_id=?", (str(arguments["id"]), context.session_id))
        if not rows:
            return ToolResult("", self.name, "任务不存在", True)
        context.store.execute("UPDATE run_tasks SET status=?,updated_at=? WHERE id=?", (str(arguments["status"]), datetime.now().astimezone().isoformat(), str(arguments["id"])))
        return ToolResult("", self.name, "已更新")


def _validate_public_url(url: str) -> None:
    """校验 URL 的所有当前 DNS 解析结果均为公网地址。

    除了直接的 ``127.0.0.1`` 或内网 IP，还要拒绝解析到私有、
    环回、链路本地或保留网段的域名。函数会对初始 URL、每次
    重定向和最终 URL 重复调用，不把一次 DNS 判定继承给新目标。

    注意：``urllib`` 在真正建立连接时可能再次解析 DNS，因此这是
    应用层防线，而不是完整的 DNS-rebinding 网络隔离。高保障模式仍需
    使用受控代理或将连接锁定到已校验 IP。
    """

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("仅支持 http/https URL")
    literal_ip = False
    try:
        ipaddress.ip_address(parsed.hostname)
        literal_ip = True
    except ValueError:
        # ``inet_aton`` 还能识别十进制整数、十六进制和省略段等旧式 IPv4 写法；
        # 仅用 ipaddress 会让 ``134744072`` 之类的 8.8.8.8 别名绕过字面量禁令。
        try:
            socket.inet_aton(parsed.hostname)
            literal_ip = True
        except OSError:
            pass
    if literal_ip:
        # 公网 IP 也必须拒绝，防止调用方绕过域名 allowlist、受控代理规则或 DNS 审计。
        raise PermissionError("拒绝直接使用 IP 地址，请使用经过授权的域名")
    # 检查 A/AAAA 等所有返回地址；只要一个内网候选就整体拒绝。
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise PermissionError("拒绝访问内网、环回或保留地址")


def _enforce_domain_allowlist(url: str, allowed_domains: tuple[str, ...]) -> None:
    """确保 URL 主机精确匹配允许域名或其真实子域。

    子域匹配显式包含 ``.`` 边界，因此 ``notexample.com`` 不会
    冒充 ``example.com``。空列表代表本层不附加域名限制，但仍需
    通过 :func:`_validate_public_url` 的公网地址校验。
    """

    if not allowed_domains:
        return
    hostname = (urllib.parse.urlparse(url).hostname or "").lower()
    if not any(hostname == domain.lower() or hostname.endswith("." + domain.lower()) for domain in allowed_domains):
        raise PermissionError(f"域名不在 allowlist：{hostname}")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """在 urllib 跟随每个 HTTP 重定向之前重新施加网络策略。

    如果只校验初始 URL，公网站点可以通过 30x 将请求引向
    云元数据地址或 localhost。因此新位置必须同时通过公网 IP 与域名
    allowlist 检查，才会交还标准库创建后续请求。
    """

    def __init__(self, allowed_domains: tuple[str, ...] = ()) -> None:
        """绑定当前能力包允许的域名集合。"""

        super().__init__()
        self.allowed_domains = allowed_domains

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        """校验新 URL 后才允许标准库跟随重定向。"""

        _validate_public_url(newurl)
        _enforce_domain_allowlist(newurl, self.allowed_domains)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass
class WebFetchTool(BaseTool):
    """抓取受控公网 HTTP(S) 页面并提取有界纯文本。

    工具在连接前、重定向前和响应后都验证 URL，同时对输入域名、
    连接时间、下载字节数和最终输出长度设限。它是轻量文本抓取器，
    不是完整 HTML 清洗或浏览器隔离器；需要执行页面时应使用容器内浏览器。
    """

    name: str = "web_fetch"
    description: str = "Fetch a public HTTP(S) page with SSRF and size protections."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]})
    risk: str = "medium"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """在线程中完成阻塞式 urllib 抓取，清理后返回文本。"""

        url = str(arguments["url"])
        await asyncio.to_thread(_validate_public_url, url)
        _enforce_domain_allowlist(url, context.allowed_domains)

        def fetch() -> tuple[str, str]:
            """执行同步 HTTP 请求；调用方必须将其放到工作线程。"""

            request = urllib.request.Request(url, headers={"User-Agent": "yy-agent/0.2"})
            opener = urllib.request.build_opener(_SafeRedirectHandler(context.allowed_domains))
            with opener.open(request, timeout=20) as response:
                final = response.geturl()
                _validate_public_url(final)
                # 多读 1 字节才能区分“恰好 2MB”和“已超限但未读完”。
                data = response.read(2_000_001)
                if len(data) > 2_000_000:
                    raise ValueError("响应超过 2MB")
                return final, data.decode("utf-8", errors="replace")

        final_url, content = await asyncio.to_thread(fetch)
        # 先去除脚本/样式再剔除标签，避免把大量非可见代码送入模型。
        content = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", content)
        content = re.sub(r"(?s)<[^>]+>", " ", content)
        content = re.sub(r"\s+", " ", content).strip()
        return ToolResult("", self.name, content[:100_000], metadata={"url": final_url})


async def _git_read(context: ToolContext, arguments: list[str]) -> ToolResult:
    """以非 shell 子进程执行一个只读 Git 子命令。

    Git 工作目录通过 ``-C`` 固定为项目根，参数以 argv 传入而不经
    shell 解析。输出会被截断，防止大型 diff/log 侵占上下文。
    本辅助函数只应由下方固定参数的 Git 工具调用。
    """

    process = await asyncio.create_subprocess_exec(
        "git", "-C", str(context.project_root), *arguments,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    content = (stdout + stderr).decode("utf-8", errors="replace")[:200_000]
    return ToolResult("", "git_" + arguments[0], content, process.returncode != 0, {"exit_code": process.returncode})


@dataclass
class GitStatusTool(BaseTool):
    """以机器易读的简短格式查看 Git 工作树和分支状态。"""

    name: str = "git_status"
    description: str = "Show Git working tree status without modifying it."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """执行 ``git status --short --branch`` 并保留退出码。"""

        del arguments
        result = await _git_read(context, ["status", "--short", "--branch"])
        return ToolResult(result.call_id, self.name, result.content, result.is_error, result.metadata)


@dataclass
class GitDiffTool(BaseTool):
    """查看受长度和路径边界限制的 Git diff。

    可选路径会先通过 :class:`PathPolicy`，再转为项目相对路径传给
    Git；``--`` 防止特殊文件名被解析为 Git 选项。本工具不写入索引。
    """

    name: str = "git_diff"
    description: str = "Show a bounded Git diff without modifying the repository."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"staged": {"type": "boolean"}, "path": {"type": "string"}}})

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """根据 ``staged`` 和可选路径构造只读 diff 命令。"""

        argv = ["diff"]
        if arguments.get("staged"):
            argv.append("--cached")
        if arguments.get("path"):
            path = PathPolicy(context.project_root).resolve(str(arguments["path"]))
            argv += ["--", str(path.relative_to(context.project_root))]
        result = await _git_read(context, argv)
        return ToolResult(result.call_id, self.name, result.content, result.is_error, result.metadata)


@dataclass
class GitLogTool(BaseTool):
    """以单行摘要列出最近 Git 提交。

    ``limit`` 被夹在 1–100 之间，既避免无意义的非正值，也防止
    完整仓库历史占满模型上下文。
    """

    name: str = "git_log"
    description: str = "Show recent Git commits without modifying the repository."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"limit": {"type": "integer"}}})

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """返回包含装饰引用的最近提交摘要。"""

        limit = min(100, max(1, int(arguments.get("limit", 20))))
        result = await _git_read(context, ["log", f"-{limit}", "--oneline", "--decorate"])
        return ToolResult(result.call_id, self.name, result.content, result.is_error, result.metadata)


@dataclass
class WebSearchTool(BaseTool):
    """通过可配置 HTML 搜索端点执行受控公网搜索。

    查询会先做 URL 编码，再复用 :class:`WebFetchTool` 的域名、SSRF、
    重定向和响应大小防护。``limit`` 只用于进一步限制交给模型的
    文本量；它不会让未受信的搜索服务突破网络策略。
    """

    name: str = "web_search"
    description: str = "Search the public web through a configurable or DuckDuckGo HTML endpoint."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]})
    risk: str = "medium"
    endpoint: str = "https://html.duckduckgo.com/html/?q={query}"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """编码查询、抓取搜索页，并按期望结果数截断文本。"""

        query = str(arguments["query"])
        url = self.endpoint.format(query=urllib.parse.quote_plus(query))
        fetched = await WebFetchTool().run({"url": url}, context)
        limit = min(20, max(1, int(arguments.get("limit", 8))))
        return ToolResult("", self.name, fetched.content[: limit * 4000], fetched.is_error, {"query": query, "endpoint": url})


@dataclass
class BrowserTool(BaseTool):
    """在专用 Playwright Docker 镜像中导航页面或生成截图。

    浏览器脚本不在宿主 Python 进程中执行。它会在 Playwright 请求层
    拦截所有资源，只允许初始主机和当前能力包中的域名，以减少
    页面对任意第三方的请求。容器仍通过高风险审批且启用网络；
    如需严格防止 DNS rebinding，还应在 Docker 网络外层配置受控代理。
    """

    name: str = "browser"
    description: str = "Use Playwright in a Docker browser image for one navigation or screenshot operation."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"url": {"type": "string"}, "operation": {"type": "string", "enum": ["text", "screenshot"]}, "output": {"type": "string"}}, "required": ["url"]})
    risk: str = "high"
    sandboxed: bool = True

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """校验目标网络边界，在容器内执行文本提取或截图。"""

        url = str(arguments["url"])
        await asyncio.to_thread(_validate_public_url, url)
        _enforce_domain_allowlist(url, context.allowed_domains)
        initial_host = urllib.parse.urlparse(url).hostname or ""
        # 初始 URL 经过公网校验后才被加入；去重和排序让容器参数稳定可审计。
        allowed_hosts = sorted(set(context.allowed_domains) | {initial_host})
        operation = str(arguments.get("operation", "text"))
        if operation == "screenshot":
            output = PathPolicy(context.project_root).resolve(str(arguments.get("output", ".yy/browser.png")), for_write=True)
            output.parent.mkdir(parents=True, exist_ok=True)
            # 脚本作为 argv 交给容器 Python，URL/路径不拼接进代码，避免脚本注入。
            script = "from playwright.sync_api import sync_playwright;import sys,json,urllib.parse;p=sync_playwright().start();b=p.chromium.launch();g=b.new_page();a=json.loads(sys.argv[3]);g.route('**/*',lambda r:r.continue_() if any((urllib.parse.urlparse(r.request.url).hostname or '')==h or (urllib.parse.urlparse(r.request.url).hostname or '').endswith('.'+h) for h in a) else r.abort());g.goto(sys.argv[1],wait_until='domcontentloaded',timeout=30000);g.screenshot(path='/workspace/'+sys.argv[2],full_page=True);b.close();p.stop()"
            relative = str(output.relative_to(context.project_root)).replace("\\", "/")
            # 固定已知 Playwright 镜像标签，避免由模型选择任意执行环境。
            browser_sandbox = DockerSandbox("mcr.microsoft.com/playwright/python:v1.44.0-jammy")
            code, stdout, stderr = await browser_sandbox.run(["python", "-c", script, url, relative, json.dumps(allowed_hosts)], cwd=str(context.project_root), writable=True, network=True, timeout=60)
            images = ()
            # 只有进程成功且目标确实存在时，才将图像附加到 ToolResult。
            if code == 0 and output.exists():
                images = (ImageContent("image/png", base64.b64encode(output.read_bytes()).decode("ascii")),)
            return ToolResult("", self.name, stdout or stderr or f"Screenshot: {relative}", code != 0, {"path": relative}, images)
        # 文本模式在容器内就截断 body，避免过大内容先穿过进程边界。
        script = "from playwright.sync_api import sync_playwright;import sys,json,urllib.parse;p=sync_playwright().start();b=p.chromium.launch();g=b.new_page();a=json.loads(sys.argv[2]);g.route('**/*',lambda r:r.continue_() if any((urllib.parse.urlparse(r.request.url).hostname or '')==h or (urllib.parse.urlparse(r.request.url).hostname or '').endswith('.'+h) for h in a) else r.abort());g.goto(sys.argv[1],wait_until='domcontentloaded',timeout=30000);print(g.locator('body').inner_text()[:100000]);b.close();p.stop()"
        browser_sandbox = DockerSandbox("mcr.microsoft.com/playwright/python:v1.44.0-jammy")
        code, stdout, stderr = await browser_sandbox.run(["python", "-c", script, url, json.dumps(allowed_hosts)], cwd=str(context.project_root), network=True, timeout=60)
        return ToolResult("", self.name, stdout or stderr, code != 0)


@dataclass
class DesktopTool(BaseTool):
    """通过 Windows UI Automation 对明确选定的窗口控件执行主机操作。

    该能力无法放入 Docker，因此固定为 ``critical`` 且 ``sandboxed=False``；
    Runtime 应对每次操作征求审批，敏感终结动作不应通过持久规则跳过。
    定位优先使用窗口标题、控件名称和 UIA 类型，不把易受分辨率和
    窗口位置影响的裸坐标作为默认策略。
    """

    name: str = "desktop"
    description: str = "Control an explicitly selected Windows UI Automation element. Always runs outside the sandbox and requires approval."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"operation": {"type": "string", "enum": ["list_windows", "screenshot", "click", "type_text"]}, "window_title": {"type": "string"}, "control_title": {"type": "string"}, "control_type": {"type": "string"}, "text": {"type": "string"}}, "required": ["operation"]})
    risk: str = "critical"
    sandboxed: bool = False

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """在 Windows 主机上列举窗口、截图、点击或向非敏感编辑控件输入。"""

        del context
        if os.name != "nt":
            return ToolResult("", self.name, "桌面控制首轮仅支持 Windows", True)
        try:
            from pywinauto import Desktop
        except ImportError:
            return ToolResult("", self.name, "请安装 yy-agent[desktop]", True)
        operation = str(arguments["operation"])
        if operation == "screenshot":
            try:
                from PIL import ImageGrab
            except ImportError:
                return ToolResult("", self.name, "请安装 yy-agent[desktop]", True)
            # 屏幕抓取是阻塞且可能较慢的主机 API，放到工作线程避免阻塞事件循环。
            image = await asyncio.to_thread(ImageGrab.grab, all_screens=True)
            import io
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return ToolResult("", self.name, "已捕获桌面截图", images=(ImageContent("image/png", base64.b64encode(buffer.getvalue()).decode("ascii")),))
        if operation == "list_windows":
            titles = await asyncio.to_thread(lambda: [window.window_text() for window in Desktop(backend="uia").windows() if window.window_text()])
            return ToolResult("", self.name, json.dumps(titles[:200], ensure_ascii=False))
        title = str(arguments.get("window_title", ""))
        if not title:
            raise ValueError("window_title 必填")
        # UIA 语义定位比鼠标坐更稳定，也能在审批界面显示有意义的目标。
        window = Desktop(backend="uia").window(title=title)
        control = window.child_window(title=str(arguments.get("control_title", "")), control_type=arguments.get("control_type"))
        if operation == "click":
            await asyncio.to_thread(control.click_input)
            return ToolResult("", self.name, "已点击控件")
        text = str(arguments.get("text", ""))
        # 输入密码是高影响且容易泄露凭据的动作，工具层再做一道不可跳过的拒绝。
        info = str(getattr(control.element_info, "control_type", "")).lower() + " " + str(getattr(control.element_info, "name", "")).lower()
        if any(marker in info for marker in ("password", "密码", "credential")):
            raise PermissionError("拒绝向密码或凭据控件输入内容")
        await asyncio.to_thread(control.set_edit_text, text)
        return ToolResult("", self.name, "已输入文本")


def default_tools(web_search_url: str | None = None) -> list[AsyncTool]:
    """构造新 Runtime 默认注册的内置工具实例列表。

    每次调用都返回全新实例，避免不同 Runtime 之间共享可变状态。
    列表包含高风险工具并不代表已授权；权限模式、CapabilityGrant 和
    每次审批仍在工具执行前决定它们是否可用。
    """

    return [
        ReadFileTool(), ReadImageTool(), ListFilesTool(), SearchTextTool(), WriteFileTool(), ApplyPatchTool(), ShellTool(),
        CalculatorTool(), CurrentTimeTool(), AskUserTool(), TaskCreateTool(), TaskListTool(), TaskUpdateTool(), GitStatusTool(), GitDiffTool(), GitLogTool(),
        WebSearchTool(endpoint=web_search_url or "https://html.duckduckgo.com/html/?q={query}"), WebFetchTool(), BrowserTool(), DesktopTool(),
    ]
