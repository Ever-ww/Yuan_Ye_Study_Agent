"""Gateway Web 工作台兼容入口。"""

from __future__ import annotations

import asyncio
import webbrowser
from pathlib import Path
from typing import Any

from Agent import default_agent_root, load_runtime_config
from gateway.api import create_gateway_api
from gateway.application import GatewayApplication
from gateway.client import GatewayClient


def create_app(token: str | None = None, *, agent_root: Path | None = None) -> Any:
    """兼容测试/嵌入调用；实际 Web 与 Gateway 使用同一 FastAPI。"""
    application = None
    if agent_root is not None:
        config = load_runtime_config(agent_root)
        application = GatewayApplication(config)
    return create_gateway_api(application, access_token=token)


def serve(port: int | None = None) -> None:
    """确保 Gateway 已启动并使用一次性地址打开浏览器。"""
    config = load_runtime_config(default_agent_root(), gateway_port=port)
    client = GatewayClient(config.agent_root, port=config.gateway_port)
    async def prepare() -> str:
        await client.register_project(Path.cwd())
        return await client.browser_url()
    url = asyncio.run(prepare())
    print(f"本机工作台：{url}")
    webbrowser.open(url)
