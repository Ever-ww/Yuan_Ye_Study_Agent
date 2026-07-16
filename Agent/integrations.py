"""可选 MCP 客户端与轻量 LSP 进程管理器。

本模块负责配置分层、协议连接和消息编解码，不负责建立信任。调用方必须只传入已信任
插件的附加配置，并在实际工具调用前经过 ``PermissionBroker``。stdio MCP 与 LSP
始终包装进 Docker；隔离后端不可用时安全失败，不会自动退回宿主机执行。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sandbox import DockerSandbox


def load_scoped_json(project_root: Path, user_dir: Path, filename: str, key: str, extra_files: list[tuple[Path, str]] | None = None) -> dict[str, Any]:
    """按用户 → 项目根 → ``.yy`` → 已信任插件顺序合并服务配置。

    后层同名项覆盖前层；插件条目强制加 ``namespace:`` 前缀，避免第三方组件覆盖用户
    或项目定义。函数只读取调用方给定文件，不扫描未知目录。
    """

    merged: dict[str, Any] = {}
    for path in (user_dir / filename, project_root / filename, project_root / ".yy" / filename):
        if not path.exists():
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        section = value.get(key, value) if isinstance(value, dict) else {}
        if isinstance(section, dict):
            merged.update(section)
    for path, namespace in extra_files or []:
        if not path.exists():
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        section = value.get(key, value) if isinstance(value, dict) else {}
        if isinstance(section, dict):
            merged.update({f"{namespace}:{name}": config for name, config in section.items()})
    return merged


def _configuration_hash(config: dict[str, Any]) -> str:
    """计算配置内容的稳定 SHA-256，供审批规则绑定真实字节语义。

    JSON 键排序和紧凑分隔符消除了文件缩进、键顺序造成的无意义差异。哈希覆盖 ``env``、
    headers 等不适合直接展示的敏感字段，因此秘密值发生变化会使既有授权失效，但授权
    描述本身不会泄露这些值。
    """

    encoded = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _explicit_environment(config: dict[str, Any]) -> dict[str, str]:
    """提取组件配置中显式声明的环境变量，拒绝含糊的非映射值。"""

    raw = config.get("env", {})
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("组件 env 必须是 JSON 对象")
    return {str(key): str(value) for key, value in raw.items()}


class MCPManager:
    """管理 MCP Server 配置并按需建立短生命周期客户端会话。"""

    def __init__(
        self,
        project_root: Path,
        user_dir: Path,
        extra_files: list[tuple[Path, str]] | None = None,
        *,
        sandbox: DockerSandbox | None = None,
    ) -> None:
        """加载各作用域 MCP 配置，不在构造阶段连接或执行服务器。

        ``sandbox`` 是可选注入点，便于 Runtime 使用自定义镜像以及测试替换可用性；
        未传入时仍创建默认 Docker 隔离器，绝不表示允许主机执行。
        """

        self.project_root = project_root
        self.user_dir = user_dir
        self.sandbox = sandbox or DockerSandbox()
        self.servers = load_scoped_json(project_root, user_dir, ".mcp.json", "mcpServers", extra_files)

    def list(self) -> list[dict[str, Any]]:
        """列出结构正确的服务配置，供 CLI/Web 展示。"""

        return [{"name": name, **config} for name, config in self.servers.items() if isinstance(config, dict)]

    def authorization_descriptor(self, name: str) -> dict[str, Any]:
        """返回不执行连接的稳定授权目标描述。

        描述包含实际 transport、命令或 URL、参数和完整配置哈希。环境变量只显示键名，
        值仅参与哈希；审批界面既能判断将运行什么，也不会把 API Key 写入事件日志。
        后续配置即使沿用同一 server 名，只要命令、URL、env 或 headers 改变，哈希就会
        改变，旧的精确授权因而无法无声覆盖新目标。
        """

        config = self.servers.get(name)
        if not isinstance(config, dict):
            raise KeyError(name)
        transport = str(config.get("type", "stdio"))
        descriptor: dict[str, Any] = {
            "transport": transport,
            "config_hash": _configuration_hash(config),
        }
        if transport == "stdio":
            command = str(config["command"])
            args = [str(value) for value in config.get("args", [])]
            environment = _explicit_environment(config)
            descriptor.update({
                "command": command,
                "args": args,
                "argv": [command, *args],
                "environment_keys": sorted(environment),
                "sandbox_image": self.sandbox.image,
                "writable": bool(config.get("writable", False)),
                "network": bool(config.get("network", False)),
            })
        elif transport in {"http", "streamable-http", "sse"}:
            descriptor["url"] = str(config["url"])
            descriptor["args"] = []
        else:
            raise ValueError(f"不支持的 MCP transport：{transport}")
        return descriptor

    async def probe(self, name: str) -> dict[str, Any]:
        """检查配置和可选 SDK 是否存在，不真正启动远端组件。"""

        descriptor = self.authorization_descriptor(name)
        if descriptor["transport"] == "stdio" and not self.sandbox.available:
            return {"name": name, "available": False, "reason": "Docker 不可用；stdio MCP 默认拒绝主机执行"}
        try:
            import mcp  # noqa: F401
        except ImportError:
            return {"name": name, "available": False, "reason": "请安装 yy-agent[mcp]"}
        return {"name": name, "available": True, "transport": descriptor["transport"]}

    async def call_tool(self, name: str, tool: str, arguments: dict[str, Any]) -> Any:
        """连接指定 MCP Server，初始化会话并调用一个工具。

        每次调用都使用上下文管理器关闭传输连接。服务配置必须在进入本方法前获得信任
        和权限审批；本方法不会自行扩大网络、进程或环境变量权限。远程 HTTP/SSE
        transport 在导入并调用 MCP SDK 前还会执行与 Web 工具相同的公网 URL 校验，
        防止配置通过 IP 字面量、localhost 或解析到内网的域名访问宿主敏感服务。
        """

        config = self.servers.get(name)
        if not isinstance(config, dict):
            raise KeyError(name)
        transport = str(config.get("type", "stdio"))
        wrapped: list[str] | None = None
        process_environment: dict[str, str] | None = None
        if transport == "stdio":
            # 在导入可选 SDK 前先证明 Docker 可用；缺少隔离时错误应明确指向安全边界，
            # 而不是被“尚未安装 mcp”掩盖，更不能尝试原始 command 的宿主机回退。
            explicit_environment = _explicit_environment(config)
            component_argv = [str(config["command"]), *[str(value) for value in config.get("args", [])]]
            wrapped = self.sandbox.wrap_argv(
                component_argv,
                cwd=str(self.project_root),
                writable=bool(config.get("writable", False)),
                network=bool(config.get("network", False)),
                environment=explicit_environment,
            )
            process_environment = self.sandbox.minimal_environment(explicit_environment)
        elif transport in {"http", "streamable-http", "sse"}:
            # 惰性导入避免 ``Agent.integrations -> tools.harness -> Agent`` 的模块初始化环。
            # 校验放在 MCP SDK 导入和 transport 上下文创建之前；IP 字面量无需 DNS，
            # 域名则检查所有 A/AAAA 结果，只要包含私网/环回/保留地址就整体拒绝。
            from tools.harness import _validate_public_url

            await asyncio.to_thread(_validate_public_url, str(config["url"]))
        try:
            from mcp import ClientSession, StdioServerParameters
        except ImportError as exc:
            raise RuntimeError("请安装 yy-agent[mcp]") from exc
        if transport == "stdio":
            from mcp.client.stdio import stdio_client
            assert wrapped is not None and process_environment is not None
            # SDK 实际启动的是 Docker CLI；原始 MCP command 仅作为容器内 argv。
            # 子进程环境由安全白名单和配置显式 env 组成，绝不复制完整 ``os.environ``。
            params = StdioServerParameters(command=wrapped[0], args=wrapped[1:], env=process_environment)
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    return await session.call_tool(tool, arguments)
        if transport in {"http", "streamable-http"}:
            from mcp.client.streamable_http import streamablehttp_client
            async with streamablehttp_client(str(config["url"]), headers=config.get("headers")) as streams:
                read_stream, write_stream = streams[0], streams[1]
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    return await session.call_tool(tool, arguments)
        if transport == "sse":
            from mcp.client.sse import sse_client
            async with sse_client(str(config["url"]), headers=config.get("headers")) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    return await session.call_tool(tool, arguments)
        raise ValueError(f"不支持的 MCP transport：{transport}")


@dataclass
class LSPProcess:
    """一个运行中 Language Server 及其下一请求序号。"""

    name: str
    process: asyncio.subprocess.Process
    sequence: int = 0


class LSPManager:
    """实现 LSP 启动与最小 JSON-RPC 请求/响应循环。"""

    def __init__(
        self,
        project_root: Path,
        user_dir: Path,
        extra_files: list[tuple[Path, str]] | None = None,
        *,
        sandbox: DockerSandbox | None = None,
    ) -> None:
        """加载配置并建立空进程表；服务仅在 ``start`` 时于 Docker 中启动。"""

        self.project_root = project_root
        self.user_dir = user_dir
        self.sandbox = sandbox or DockerSandbox()
        self.servers = load_scoped_json(project_root, user_dir, ".lsp.json", "lspServers", extra_files)
        self.processes: dict[str, LSPProcess] = {}

    def list(self) -> list[dict[str, Any]]:
        """列出可配置的 Language Server，不改变进程状态。"""

        return [{"name": name, **config} for name, config in self.servers.items() if isinstance(config, dict)]

    def authorization_descriptor(self, name: str) -> dict[str, Any]:
        """返回 LSP 容器启动目标的稳定、无副作用授权描述。"""

        config = self.servers.get(name)
        if not isinstance(config, dict):
            raise KeyError(name)
        command = str(config["command"])
        args = [str(value) for value in config.get("args", [])]
        environment = _explicit_environment(config)
        return {
            "transport": "stdio",
            "command": command,
            "args": args,
            "argv": [command, *args],
            "config_hash": _configuration_hash(config),
            "environment_keys": sorted(environment),
            "sandbox_image": self.sandbox.image,
            "writable": bool(config.get("writable", False)),
            "network": bool(config.get("network", False)),
        }

    async def start(self, name: str) -> None:
        """幂等地在 Docker 内启动服务并发送标准 ``initialize`` 请求。

        同名服务已运行时直接返回，保证一个管理器内不会重复启动。Docker CLI 不可用
        时 ``wrap_argv`` 直接安全失败，不会把原始 Language Server 命令交给宿主机。
        """

        config = self.servers.get(name)
        if not isinstance(config, dict):
            raise KeyError(name)
        if name in self.processes:
            return
        explicit_environment = _explicit_environment(config)
        component_argv = [str(config["command"]), *[str(value) for value in config.get("args", [])]]
        wrapped = self.sandbox.wrap_argv(
            component_argv,
            cwd=str(self.project_root),
            writable=bool(config.get("writable", False)),
            network=bool(config.get("network", False)),
            environment=explicit_environment,
        )
        process = await asyncio.create_subprocess_exec(
            *wrapped,
            cwd=str(self.project_root), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=self.sandbox.minimal_environment(explicit_environment),
        )
        self.processes[name] = LSPProcess(name, process)
        # 容器内始终从 /workspace 观察仓库，初始化 URI 不能泄露或引用宿主绝对路径。
        await self.request(name, "initialize", {"processId": None, "rootUri": "file:///workspace", "capabilities": {}})

    async def request(self, name: str, method: str, params: dict[str, Any]) -> Any:
        """发送一个 LSP JSON-RPC 请求，并等待匹配当前序号的响应。

        LSP 使用 ``Content-Length`` 帧。通知或其他响应会在循环中跳过；每条 header
        最多等待 30 秒，服务返回 JSON-RPC error 时转换为 ``RuntimeError``。
        """

        server = self.processes[name]
        server.sequence += 1
        payload = json.dumps({"jsonrpc": "2.0", "id": server.sequence, "method": method, "params": params}).encode("utf-8")
        assert server.process.stdin and server.process.stdout
        server.process.stdin.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload)
        await server.process.stdin.drain()
        while True:
            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(server.process.stdout.readline(), timeout=30)
                if line in {b"\r\n", b"\n", b""}:
                    break
                key, value = line.decode("ascii", errors="replace").split(":", 1)
                headers[key.lower()] = value.strip()
            body = await server.process.stdout.readexactly(int(headers["content-length"]))
            message = json.loads(body)
            # Language Server 可能在响应前发送 diagnostics 等通知，忽略非当前请求 ID。
            if message.get("id") == server.sequence:
                if "error" in message:
                    raise RuntimeError(str(message["error"]))
                return message.get("result")

    async def stop_all(self) -> None:
        """终止所有已启动服务、等待进程退出并清空注册表。"""

        for server in self.processes.values():
            server.process.terminate()
        await asyncio.gather(*(server.process.wait() for server in self.processes.values()), return_exceptions=True)
        self.processes.clear()
