"""安全失败的 Docker 命令沙箱与必须逐次审批的主机回退执行器。

两种执行器都使用 ``create_subprocess_exec`` 直接传递参数数组，不经过 Shell 解析。
Docker 不可用时默认抛出 ``SandboxUnavailable``，绝不会静默降级到主机执行。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Mapping


class SandboxUnavailable(RuntimeError):
    """请求隔离执行但当前机器没有可用 Docker 时抛出的明确错误。"""


class DockerSandbox:
    """通过临时、降权、资源受限的 Docker 容器执行命令。"""

    def __init__(self, image: str = "python:3.12-slim", *, allow_unsandboxed: bool = False) -> None:
        """保存镜像及兼容配置；本类自身不会自动使用非隔离回退。"""

        self.image = image
        self.allow_unsandboxed = allow_unsandboxed

    @property
    def available(self) -> bool:
        """仅检测 Docker CLI 是否在 PATH；守护进程错误会在真正执行时返回。"""

        return shutil.which("docker") is not None

    @staticmethod
    def minimal_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """构造启动隔离进程所需的最小宿主环境。

        Docker CLI 本身只需要少量路径、Windows 系统目录、临时目录和区域设置变量。
        API Key、云凭据和用户自定义秘密不会从 :data:`os.environ` 自动继承。调用方
        可以通过 ``extra`` 显式加入已经过信任审查的组件配置；这些值仅属于本次子
        进程，不会回写当前 Python 进程的环境。

        环境变量名称采用各平台共同支持的保守格式。拒绝包含 ``=``、空字节或其他
        控制字符的名称，可以避免 Docker ``--env`` 参数和底层进程 API 对键边界产生
        不同解释。
        """

        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
        # Windows 环境变量名大小写不敏感，实际枚举结果可能是 ``Path`` 或
        # ``SystemRoot``；按大写比较但保留原键名，兼顾 Windows 与 POSIX 子进程语义。
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        for raw_key, raw_value in (extra or {}).items():
            key = str(raw_key)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"无效环境变量名称：{key!r}")
            value = str(raw_value)
            if "\x00" in value:
                raise ValueError(f"环境变量 {key} 包含空字节")
            environment[key] = value
        return environment

    def wrap_argv(
        self,
        argv: list[str],
        *,
        cwd: str,
        writable: bool = False,
        network: bool = False,
        environment: Mapping[str, str] | None = None,
    ) -> list[str]:
        """把组件命令包装为可交给任意 stdio 客户端启动的 Docker argv。

        该方法只构造参数，不启动进程，因此 MCP SDK、LSP 管理器和普通 Shell 工具
        可以共享完全相同的容器边界。``environment`` 中只写入变量名称；真实值通过
        :meth:`minimal_environment` 形成的宿主子进程环境传给 Docker，避免秘密直接
        出现在命令行、授权描述或进程列表中。

        Docker CLI 不存在时立即抛出 :class:`SandboxUnavailable`。调用方不得捕获后
        自动改用主机命令；需要非隔离执行时必须走独立、逐次审批的主机执行器。
        """

        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise ValueError("命令必须是非空字符串数组")
        if not self.available:
            raise SandboxUnavailable("Docker 不可用；默认拒绝执行 Shell 或第三方代码")
        root = Path(cwd).resolve()
        mount_mode = "rw" if writable else "ro"
        command = [
            "docker", "run", "--rm", "--init", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128",
            "--memory=1g", "--cpus=2", "--workdir=/workspace",
            "--mount", f"type=bind,src={root},dst=/workspace,{mount_mode}",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
            "--network", "bridge" if network else "none",
        ]
        # ``--env KEY`` 只从经过清理的 Docker 客户端环境取值，命令行中不出现秘密值。
        for key in sorted((environment or {}).keys()):
            # 复用同一校验逻辑，但不需要在此保留返回的环境副本。
            self.minimal_environment({str(key): str((environment or {})[key])})
            command.extend(["--env", str(key)])
        command.extend([self.image, *argv])
        return command

    async def run(
        self,
        argv: list[str],
        *,
        cwd: str,
        writable: bool = False,
        network: bool = False,
        timeout: float = 120,
    ) -> tuple[int, str, str]:
        """在只读根文件系统和最小能力集合中运行参数数组。

        工作区默认只读挂载，写能力须由上层审批后显式传入；网络默认完全关闭。
        即使启用网络，上层仍应通过代理与域名白名单约束具体目标。
        """

        # 不使用 ``shell=True``，因此 argv 中的分号、管道等不会被解释为控制操作符。
        command = self.wrap_argv(argv, cwd=cwd, writable=writable, network=network)
        return await _run_process(command, cwd=str(Path(cwd).resolve()), timeout=timeout, clean_environment=True)


class HostProcessRunner:
    """非隔离主机执行器；调用方必须在每次使用前获得明确审批。

    ``writable`` 与 ``network`` 无法在主机层强制隔离，因此这里只接受参数以实现统一
    接口，实际安全边界完全依赖上层权限代理，不能作为 Docker 自动回退。
    """

    @property
    def available(self) -> bool:
        """主机进程能力始终存在，但存在不代表已经获得使用授权。"""

        return True

    async def run(
        self,
        argv: list[str],
        *,
        cwd: str,
        writable: bool = False,
        network: bool = False,
        timeout: float = 120,
    ) -> tuple[int, str, str]:
        """以清理后的环境直接启动进程；忽略无法强制的能力提示参数。"""

        del writable, network
        return await _run_process(argv, cwd=cwd, timeout=timeout, clean_environment=True)


async def _run_process(argv: list[str], *, cwd: str, timeout: float, clean_environment: bool) -> tuple[int, str, str]:
    """执行参数数组，收集有限生命周期内的 stdout/stderr。

    超时后先终止进程再排空管道，避免遗留僵尸进程。输出以 UTF-8 容错解码，确保
    非 UTF-8 字节不会破坏事件序列化。
    """

    env = None
    if clean_environment:
        # 明确白名单可阻断 API Key、云凭据和用户自定义秘密环境变量泄露给子进程。
        env = DockerSandbox.minimal_environment()
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise TimeoutError(f"命令执行超过 {timeout} 秒")
    return process.returncode or 0, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
