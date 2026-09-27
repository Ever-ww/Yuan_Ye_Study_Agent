"""Native local browser-use adapter for the Main Agent.

The adapter deliberately exposes deterministic browser actions instead of
starting browser-use's second LLM agent.  YYAgent remains the only planner;
browser-use owns the local browser session and CDP/Playwright mechanics.
"""

from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from tool.contracts import ToolContext


class BrowserUseUnavailableError(RuntimeError):
    """Raised when the optional browser-use runtime is not installed."""


class BrowserUseTool:
    """Control one workspace-scoped local browser session.

    The browser process is lazy and remains alive while the RuntimePool keeps
    the Main Agent runtime alive.  It is killed when that runtime is closed.
    """

    name = "browser_use"
    description = (
        "使用本地 browser-use 浏览器执行网页操作。先用 state 查看当前页面和可交互元素，"
        "再按需打开网址、点击、填写、滚动或提取正文。会改变网页状态的操作需要审批。"
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "state", "open", "click", "fill", "press", "scroll",
                    "back", "tabs", "switch_tab", "close_tab", "extract",
                ],
            },
            "url": {"type": "string", "maxLength": 4000},
            "index": {"type": "integer", "minimum": 0, "maximum": 10000},
            "tab_index": {"type": "integer", "minimum": 0, "maximum": 1000},
            "text": {"type": "string", "maxLength": 20000},
            "key": {"type": "string", "maxLength": 100},
            "direction": {"type": "string", "enum": ["up", "down"]},
            "new_tab": {"type": "boolean"},
            "max_chars": {"type": "integer", "minimum": 100, "maximum": 50000},
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    risk = "dynamic"
    idempotency = "NON_IDEMPOTENT"
    parallel_safe = False
    extension_preapproval = False
    runtime_profiles = ("interactive",)

    def __init__(
        self,
        workspace_root: Path,
        *,
        agent_root: Path | None = None,
        headless: bool = False,
        timeout_seconds: float = 60.0,
        allowed_domains: tuple[str, ...] = (),
        allow_private_urls: bool = False,
        session_factory: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.agent_root = (agent_root or workspace_root).resolve()
        self.headless = headless
        self.timeout_seconds = max(5.0, min(float(timeout_seconds), 600.0))
        self.allowed_domains = tuple(item.lower().strip() for item in allowed_domains if item.strip())
        self.allow_private_urls = allow_private_urls
        self._session: Any | None = None
        self._session_lock = asyncio.Lock()
        self._call_lock = asyncio.Lock()
        self._session_factory = session_factory

    def is_available(self, context: ToolContext | None = None) -> bool:
        del context
        return importlib.util.find_spec("browser_use") is not None or self._session_factory is not None

    def risk_for(self, arguments: dict[str, Any]) -> str:
        action = str(arguments.get("action", "")).strip().lower()
        if action in {"open", "click", "fill", "press", "close_tab"}:
            return "high"
        return "read"

    def ensure_available(self, arguments: dict[str, Any], context: ToolContext) -> None:
        """Reject malformed browser actions before YYAgent requests approval."""
        del context
        action = str(self._require(arguments, "action")).strip().lower()
        if action == "open":
            self._validate_url(str(self._require(arguments, "url")))
        elif action in {"click", "fill"}:
            self._require(arguments, "index")
            if action == "fill":
                self._require(arguments, "text")
        elif action == "press":
            self._require(arguments, "key")
        elif action == "switch_tab":
            self._require(arguments, "tab_index")

    @staticmethod
    def _require(arguments: dict[str, Any], name: str) -> Any:
        value = arguments.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(f"browser_use.{name} 是必需参数")
        return value

    def _validate_url(self, value: str) -> str:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("browser_use 只允许 http:// 或 https:// URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("browser_use URL 不允许嵌入用户名或密码")
        host = parsed.hostname.lower().rstrip(".")
        if not self.allow_private_urls:
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address is not None and (address.is_private or address.is_loopback or address.is_link_local):
                raise ValueError("browser_use 默认拒绝本机或内网地址；需要时显式启用 allow_private_urls")
            if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal")):
                raise ValueError("browser_use 默认拒绝本机或内网主机名")
        if self.allowed_domains and not any(
            host == item or (item.startswith("*.") and host.endswith(item[1:]))
            for item in self.allowed_domains
        ):
            raise ValueError("browser_use URL 不在允许的域名范围内")
        return value.strip()

    async def _default_session_factory(self) -> Any:
        profile_dir = self.workspace_root / ".yy" / "browser-use" / "profile"
        config_dir = self.agent_root / ".yy" / "browser-use"
        profile_dir.mkdir(parents=True, exist_ok=True)
        config_dir.mkdir(parents=True, exist_ok=True)
        # browser-use caches its config during import. Keep process-wide
        # control data in YYAgent Home; browser identity stays per Workspace.
        os.environ.setdefault("BROWSER_USE_CONFIG_DIR", str(config_dir))
        os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
        os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")
        from browser_use import Browser

        return Browser(
            headless=self.headless,
            user_data_dir=profile_dir,
            keep_alive=True,
            enable_default_extensions=False,
            accept_downloads=False,
            auto_download_pdfs=False,
            disable_security=False,
            use_cloud=False,
            allowed_domains=list(self.allowed_domains) or None,
        )

    async def _browser(self) -> Any:
        if self._session is not None:
            return self._session
        async with self._session_lock:
            if self._session is None:
                if self._session_factory is not None:
                    self._session = await self._session_factory()
                else:
                    self._session = await self._default_session_factory()
                try:
                    await asyncio.wait_for(self._session.start(), self.timeout_seconds)
                except BaseException:
                    session, self._session = self._session, None
                    if session is not None:
                        try:
                            await session.kill()
                        except Exception:
                            pass
                    raise
        return self._session

    async def _page(self, browser: Any) -> Any:
        page = await browser.get_current_page()
        if page is None:
            page = await browser.new_page()
        return page

    @staticmethod
    def _json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        action = str(self._require(arguments, "action")).strip().lower()
        self.ensure_available(arguments, context)
        async with self._call_lock:
            try:
                browser = await self._browser()
                if action == "open":
                    url = self._validate_url(str(self._require(arguments, "url")))
                    await asyncio.wait_for(
                        browser.navigate_to(url, new_tab=bool(arguments.get("new_tab", False))),
                        self.timeout_seconds,
                    )
                    return self._json({"action": action, "url": url, "status": "opened"})
                if action == "state":
                    text = await asyncio.wait_for(browser.get_state_as_text(), self.timeout_seconds)
                    return self._json({
                        "action": action, "state": text[:50000],
                        "untrusted_external_content": True,
                    })
                if action == "tabs":
                    tabs = await asyncio.wait_for(browser.get_tabs(), self.timeout_seconds)
                    return self._json({
                        "action": action,
                        "tabs": [tab.model_dump(mode="json") if hasattr(tab, "model_dump") else str(tab) for tab in tabs],
                    })
                if action == "back":
                    page = await self._page(browser)
                    await asyncio.wait_for(page.go_back(), self.timeout_seconds)
                    return self._json({"action": action, "status": "completed", "url": await page.get_url()})
                if action == "switch_tab":
                    tabs = await browser.get_tabs()
                    tab_index = int(self._require(arguments, "tab_index"))
                    if tab_index < 0 or tab_index >= len(tabs):
                        raise ValueError("browser_use.tab_index 超出当前标签页范围")
                    target_id = tabs[tab_index].target_id
                    from browser_use.browser.events import SwitchTabEvent
                    await asyncio.wait_for(
                        browser.on_SwitchTabEvent(SwitchTabEvent(target_id=target_id)),
                        self.timeout_seconds,
                    )
                    return self._json({"action": action, "tab_index": tab_index, "status": "completed"})
                if action == "close_tab":
                    page = await self._page(browser)
                    await asyncio.wait_for(browser.close_page(page), self.timeout_seconds)
                    return self._json({"action": action, "status": "closed"})
                page = await self._page(browser)
                if action == "click":
                    node = await browser.get_element_by_index(int(self._require(arguments, "index")))
                    if node is None or getattr(node, "backend_node_id", None) is None:
                        raise ValueError("browser_use 找不到对应的可交互元素")
                    element = await page.get_element(node.backend_node_id)
                    await asyncio.wait_for(element.click(), self.timeout_seconds)
                    return self._json({"action": action, "status": "completed"})
                if action == "fill":
                    node = await browser.get_element_by_index(int(self._require(arguments, "index")))
                    if node is None or getattr(node, "backend_node_id", None) is None:
                        raise ValueError("browser_use 找不到对应的可交互元素")
                    element = await page.get_element(node.backend_node_id)
                    await asyncio.wait_for(element.fill(str(self._require(arguments, "text"))), self.timeout_seconds)
                    return self._json({"action": action, "status": "completed"})
                if action == "press":
                    await asyncio.wait_for(page.press(str(self._require(arguments, "key"))), self.timeout_seconds)
                    return self._json({"action": action, "status": "completed"})
                if action == "scroll":
                    direction = str(arguments.get("direction", "down")).lower()
                    if direction not in {"up", "down"}:
                        raise ValueError("browser_use.direction 必须是 up 或 down")
                    amount = -800 if direction == "up" else 800
                    await asyncio.wait_for(
                        page.evaluate(f"() => {{ window.scrollBy(0, {amount}); return true; }}"),
                        self.timeout_seconds,
                    )
                    return self._json({"action": action, "direction": direction, "status": "completed"})
                if action == "extract":
                    text = await asyncio.wait_for(
                        page.evaluate("() => document.body ? document.body.innerText : ''"),
                        self.timeout_seconds,
                    )
                    max_chars = min(max(100, int(arguments.get("max_chars", 30000))), 50000)
                    return self._json({
                        "action": action, "content": str(text)[:max_chars],
                        "untrusted_external_content": True,
                    })
                raise ValueError(f"不支持的 browser_use action: {action}")
            except asyncio.TimeoutError as exc:
                raise TimeoutError(f"browser_use 操作超过 {self.timeout_seconds:.0f} 秒") from exc
            except ImportError as exc:
                raise BrowserUseUnavailableError(
                    "browser-use 未安装，请使用 Python 3.13 执行 uv sync",
                ) from exc
            except (ValueError, TimeoutError):
                raise
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                message = message.replace(str(self.workspace_root), "YYWorkspace:\\")
                message = context.sanitize_model_output(message)
                message = message.replace(str(Path.home()), "<user-home>")
                raise RuntimeError(f"browser_use 操作失败：{type(exc).__name__}: {message[:500]}") from exc

    async def close(self) -> None:
        async with self._session_lock:
            session, self._session = self._session, None
            if session is not None:
                try:
                    await session.kill()
                except Exception:
                    # Cleanup must not hide the Runtime result.
                    pass
