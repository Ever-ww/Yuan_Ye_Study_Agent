"""仅监听本机回环地址的 FastAPI 管理界面。

Web 层复用 :class:`Agent.runtime.AgentRuntime`，不复制权限判断或工具执行逻辑。浏览器只
负责提交任务、消费事件，以及把人工审批/提问通过 Future 回传给 Runtime。启动令牌用于
请求认证，CSRF 令牌额外保护会改变审批状态的 HTTP 接口。

注意：绑定地址的强制限制位于 :func:`run_ui.cli.serve`；直接调用 ``create_app()`` 只会
构造 ASGI 应用，部署者仍必须确保服务器只监听可信回环地址。
"""

from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from Agent.config import RuntimeConfig, load_runtime_config
from Agent.permissions import ApprovalDecision, PermissionRequest
from Agent.runtime import AgentRuntime


class WebService:
    """保存单个 Web 进程共享的 Runtime、认证令牌和待响应队列。"""

    def __init__(self, config: RuntimeConfig, token: str | None = None) -> None:
        """创建服务状态，并把 Web 审批回调注入统一 Runtime。"""
        self.config = config
        # token_urlsafe 使用系统安全随机源；显式 token 仅用于测试或受控嵌入场景。
        self.token = token or secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(24)
        # Future 把“Runtime 正在等待”的协程和“浏览器稍后 POST 回答”的请求连接起来。
        self.pending: dict[str, tuple[PermissionRequest, asyncio.Future[ApprovalDecision]]] = {}
        self.questions: dict[str, tuple[str, list[str], asyncio.Future[str]]] = {}
        self.runtime = AgentRuntime(config, approval_callback=self.request_approval, question_callback=self.request_question)

    async def request_approval(self, request: PermissionRequest) -> ApprovalDecision:
        """登记一次待审批调用并等待浏览器答复，超时默认拒绝。"""
        request_id = uuid4().hex
        future: asyncio.Future[ApprovalDecision] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = (request, future)
        try:
            return await asyncio.wait_for(future, timeout=300)
        except asyncio.TimeoutError:
            # 审批超时采用 fail-closed，绝不能因为 UI 无响应而自动放行工具。
            return ApprovalDecision.DENY
        finally:
            # 无论正常、超时还是请求协程被取消，都移除已失效的 Future。
            self.pending.pop(request_id, None)

    async def request_question(self, question: str, choices: list[str]) -> str:
        """登记 Agent 向用户提出的问题，并最多等待十分钟。"""
        request_id = uuid4().hex
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.questions[request_id] = (question, choices, future)
        try:
            return await asyncio.wait_for(future, timeout=600)
        except asyncio.TimeoutError:
            return "User did not answer before timeout."
        finally:
            self.questions.pop(request_id, None)


def create_app(config: RuntimeConfig | None = None, *, token: str | None = None) -> Any:
    """创建 FastAPI 应用。

    FastAPI 在函数内部导入，使只使用核心 Runtime 或旧 CLI 的用户不必安装 Web 依赖。
    返回值标为 ``Any``，避免核心包为了类型标注而硬依赖 FastAPI。
    """
    try:
        from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
        from fastapi.responses import HTMLResponse
    except ImportError as exc:
        raise RuntimeError("Web UI 需要安装 FastAPI 与 Uvicorn") from exc

    # FastAPI 会通过模块全局命名空间解析 postponed annotations；局部导入的 Request 和
    # WebSocket 若不写回 globals，路由注册时可能出现 forward reference 解析失败。
    globals().update({"Request": Request, "WebSocket": WebSocket})

    service = WebService(config or load_runtime_config(), token)
    app = FastAPI(title="Yuan Ye Agent", docs_url=None, redoc_url=None)
    app.state.yy_service = service

    def check_token(request: Request) -> None:
        """校验 Authorization Bearer 或查询参数中的启动令牌。"""
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ") or request.query_params.get("token", "")
        # compare_digest 降低普通字符串比较泄露前缀匹配时间的风险。
        if not secrets.compare_digest(supplied, service.token):
            raise HTTPException(401, "invalid token")

    def check_csrf(value: str | None) -> None:
        """校验改变审批/回答状态的请求携带的 CSRF 令牌。"""
        if not value or not secrets.compare_digest(value, service.csrf):
            raise HTTPException(403, "invalid csrf token")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        """为所有 HTTP 响应添加浏览器侧最小安全头。"""
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        # 页面内存中含启动 token 和 CSRF token；浏览器、代理和历史缓存都不得持久保存。
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self' ws://127.0.0.1:*"
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> str:
        """返回内嵌的单页管理界面；访问首页也必须持有 token。"""
        check_token(request)
        return _html(service.token, service.csrf)

    @app.get("/api/status")
    async def status(request: Request) -> dict[str, Any]:
        """汇总 UI 首页所需的项目、沙箱和扩展状态。"""
        check_token(request)
        return {
            "project": str(service.config.project_root),
            "sandbox": {"enabled": service.config.sandbox.enabled, "available": service.runtime.sandbox.available},
            "sessions": service.runtime.store.list_sessions(20),
            "skills": [{"name": item.qualified_name, "description": item.description} for item in service.runtime.skills.discover([Path(row["installed_path"]) for row in service.runtime.plugins.installed(enabled_only=True)])],
            "plugins": service.runtime.plugins.installed(),
            "hooks": service.runtime.hooks.describe(),
            "cron": service.runtime.scheduler.list_schedules(),
        }

    @app.get("/api/memory")
    async def memory(request: Request, query: str = "") -> list[dict[str, Any]]:
        """列出全部有效记忆，或按查询词执行 FTS 检索。"""
        check_token(request)
        return service.runtime.memory.search(query) if query else service.runtime.memory.list()

    @app.get("/api/corpus")
    async def corpus(request: Request, query: str) -> list[dict[str, Any]]:
        """检索独立学习资料库，不与长期记忆混合。"""
        check_token(request)
        return service.runtime.corpus.search(query)

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(session_id: str, request: Request) -> list[dict[str, Any]]:
        """按落库顺序返回指定会话的完整事件轨迹。"""
        check_token(request)
        return service.runtime.store.events(session_id)

    @app.get("/api/approvals")
    async def approvals(request: Request) -> list[dict[str, Any]]:
        """返回当前仍在等待人工决定的权限请求。"""
        check_token(request)
        return [{"id": key, **asdict(value[0])} for key, value in service.pending.items()]

    @app.post("/api/approvals/{request_id}")
    async def resolve_approval(request_id: str, request: Request, x_yy_csrf: str | None = Header(default=None)) -> dict[str, bool]:
        """验证输入后完成对应审批 Future，使 Runtime 恢复执行。"""
        check_token(request)
        check_csrf(x_yy_csrf)
        body = await request.json()
        try:
            decision = ApprovalDecision(str(body["decision"]))
            _, future = service.pending[request_id]
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, "approval not found or decision invalid") from exc
        if not future.done():
            # 重复提交不会二次 set_result；已完成的请求仍返回幂等成功响应。
            future.set_result(decision)
        return {"ok": True}

    @app.get("/api/questions")
    async def questions(request: Request) -> list[dict[str, Any]]:
        """返回 Agent 正在等待用户回答的问题。"""
        check_token(request)
        return [{"id": key, "question": value[0], "choices": value[1]} for key, value in service.questions.items()]

    @app.post("/api/questions/{request_id}")
    async def answer_question(request_id: str, request: Request, x_yy_csrf: str | None = Header(default=None)) -> dict[str, bool]:
        """提交用户答案并唤醒 ``ask_user`` 工具对应的 Future。"""
        check_token(request)
        check_csrf(x_yy_csrf)
        body = await request.json()
        try:
            _, _, future = service.questions[request_id]
            answer = str(body["answer"])
        except KeyError as exc:
            raise HTTPException(404, "question not found") from exc
        if not future.done():
            future.set_result(answer)
        return {"ok": True}

    @app.websocket("/ws/chat")
    async def chat(socket: WebSocket) -> None:
        """在一个 WebSocket 上串行接收任务并推送 Runtime 事件。"""
        supplied = socket.query_params.get("token", "")
        if not secrets.compare_digest(supplied, service.token):
            await socket.close(code=4401)
            return
        await socket.accept()
        try:
            while True:
                request = await socket.receive_json()
                task = str(request.get("task", ""))
                session_id = request.get("session_id")
                # 直接转发结构化事件，前端无需猜测模型文本与工具结果的边界。
                async for event in service.runtime.run_turn(task, session_id=session_id):
                    await socket.send_json(event.to_dict())
        except WebSocketDisconnect:
            return

    return app


def _html(token: str, csrf: str) -> str:
    """生成无需 Node 构建链的内嵌 HTML/CSS/JavaScript 页面。

    token 与 csrf 使用 ``json.dumps`` 注入 JavaScript 字符串，避免手工转义导致脚本注入。
    页面只提供最小管理能力；复杂前端可复用同一组受认证 API。
    """
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Yuan Ye Agent</title><style>
body{{font-family:system-ui;margin:0;background:#0f172a;color:#e2e8f0}}main{{max-width:1000px;margin:auto;padding:24px}}
#log{{white-space:pre-wrap;background:#111827;border:1px solid #334155;border-radius:10px;padding:16px;height:55vh;overflow:auto}}
textarea{{width:100%;min-height:90px;background:#1e293b;color:#fff;border:1px solid #475569;border-radius:8px;padding:10px;box-sizing:border-box}}
button{{background:#22c55e;border:0;border-radius:7px;padding:9px 14px;margin:6px;color:#052e16;cursor:pointer}}.deny{{background:#fb7185}}.card{{border:1px solid #475569;padding:10px;margin:8px 0;border-radius:8px}}
</style></head><body><main><h1>Yuan Ye Agent</h1><div id="status"></div><div id="approvals"></div><div id="log"></div>
<textarea id="task" placeholder="输入任务"></textarea><button onclick="sendTask()">发送</button></main><script>
// token 只驻留当前页面内存；sessionId 用于把后续任务接到同一会话。
const token={json.dumps(token)}, csrf={json.dumps(csrf)}; let sessionId=null;
const ws=new WebSocket(`ws://${{location.host}}/ws/chat?token=${{encodeURIComponent(token)}}`);
// 服务端事件保持原始 JSON 展示，便于调试审批、工具和模型生命周期。
ws.onmessage=e=>{{const v=JSON.parse(e.data);sessionId=v.session_id||sessionId;document.querySelector('#log').textContent+=`\n[${{v.type}}] ${{JSON.stringify(v.payload)}}`;}};
function sendTask(){{const task=document.querySelector('#task').value;if(task)ws.send(JSON.stringify({{task,session_id:sessionId}}));}}
// 短轮询仅获取管理状态；模型事件通过 WebSocket 推送，不依赖轮询。
async function poll(){{let s=await fetch('/api/status?token='+encodeURIComponent(token)).then(r=>r.json());document.querySelector('#status').textContent=`项目: ${{s.project}} · Docker: ${{s.sandbox.available?'可用':'不可用'}}`;
let a=await fetch('/api/approvals?token='+encodeURIComponent(token)).then(r=>r.json());let q=await fetch('/api/questions?token='+encodeURIComponent(token)).then(r=>r.json());renderPending(a,q);}}
function renderPending(approvals,questions){{const box=document.querySelector('#approvals');box.replaceChildren();for(const x of approvals){{const card=document.createElement('div');card.className='card';card.append(document.createTextNode('审批 '+x.tool+' '+JSON.stringify(x.arguments)));const yes=document.createElement('button');yes.textContent='允许一次';yes.onclick=()=>approve(x.id,'allow-once');const no=document.createElement('button');no.textContent='拒绝';no.className='deny';no.onclick=()=>approve(x.id,'deny');card.append(yes,no);box.append(card);}}for(const x of questions){{const card=document.createElement('div');card.className='card';card.append(document.createTextNode('Agent 提问：'+x.question+' '+x.choices.join(' / ')));const button=document.createElement('button');button.textContent='回答';button.onclick=()=>answer(x.id,x.question);card.append(button);box.append(card);}}}}
async function approve(id,decision){{await fetch('/api/approvals/'+id+'?token='+encodeURIComponent(token),{{method:'POST',headers:{{'Content-Type':'application/json','X-YY-CSRF':csrf}},body:JSON.stringify({{decision}})}});poll();}}setInterval(poll,1500);poll();
async function answer(id,q){{let value=prompt(q);if(value!==null)await fetch('/api/questions/'+id+'?token='+encodeURIComponent(token),{{method:'POST',headers:{{'Content-Type':'application/json','X-YY-CSRF':csrf}},body:JSON.stringify({{answer:value}})}});poll();}}
</script></body></html>"""
