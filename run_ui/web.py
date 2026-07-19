"""本机 Web 聊天工作台与安全事件桥接。"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from Agent import AgentRuntime


def create_app(token: str | None = None) -> Any:
    """创建固定本机使用的 FastAPI 应用。"""
    try:
        from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
        from fastapi.responses import FileResponse
    except ModuleNotFoundError as exc:
        raise RuntimeError("Web UI 需要安装 fastapi 与 uvicorn") from exc
    access_token, csrf = token or secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    base = Path(__file__).parent
    app = FastAPI()

    def authorize(value: str | None) -> None:
        if not value or not secrets.compare_digest(value, access_token):
            raise HTTPException(401, "无效访问令牌")

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'self'; connect-src 'self' ws:; style-src 'self'"})
        return response

    @app.get("/")
    async def index(token: str):
        authorize(token)
        return FileResponse(base / "templates" / "index.html", media_type="text/html")

    @app.get("/assets/{name}")
    async def assets(name: str, token: str):
        authorize(token)
        return FileResponse(base / "static" / name)

    @app.get("/api/bootstrap")
    async def bootstrap(token: str):
        authorize(token)
        return {"csrf": csrf}

    @app.websocket("/ws/chat")
    async def chat(socket: WebSocket, token: str):
        if not secrets.compare_digest(token, access_token):
            await socket.close(code=1008)
            return
        await socket.accept()
        runtime = AgentRuntime()
        try:
            while True:
                payload = await socket.receive_json()
                async for event in runtime.run_task(str(payload.get("task", "")), payload.get("session_id")):
                    await socket.send_json({"type": event.type.value, "payload": event.payload})
        except WebSocketDisconnect:
            return
        finally:
            await runtime.close()

    app.state.access_token = access_token
    return app


def serve(port: int) -> None:
    """启动本机服务并输出仅当前用户可用的访问地址。"""
    import uvicorn
    app = create_app()
    print(f"本机工作台：http://127.0.0.1:{port}/?token={app.state.access_token}")
    uvicorn.run(app, host="127.0.0.1", port=port)
