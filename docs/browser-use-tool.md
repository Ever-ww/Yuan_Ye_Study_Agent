# Main Agent 本地 Browser Use 工具

YYAgent 通过原生 `BrowserUseTool` 调用 [browser-use](https://github.com/browser-use/browser-use) 的 `BrowserSession`，不启动 MCP，也不再创建第二个浏览器 Agent。Main Agent 负责决定下一步，browser-use 只负责本地浏览器和 CDP/Playwright 操作。

## 安装

项目运行时固定为 Python 3.13：

```powershell
uv python install 3.13
uv sync
```

首次使用时会在 YYAgent Home 的 `.yy/browser-use/` 下创建全局浏览器控制配置，在当前 Workspace 的 `.yy/browser-use/profile/` 下创建用户数据。不同 Workspace 不共享浏览器 Profile。
browser-use 启动时检测本机 Chrome、Chromium 或 Edge；若未找到浏览器，工具会返回明确错误。无需启动 browser-use 自带的 CLI 或 MCP 服务。

## 工具动作

Main Agent 可调用 `browser_use`，动作包括：

- `state`：读取当前页面和可交互元素索引。
- `open`：打开 HTTP(S) 页面，可选 `new_tab`。
- `click`、`fill`、`press`：按索引操作页面。
- `scroll`、`back`：浏览页面。
- `tabs`、`switch_tab`、`close_tab`：管理标签页。
- `extract`：提取当前页面正文，不额外调用 LLM。

`open`、`click`、`fill`、`press` 和 `close_tab` 继续经过 YYAgent 的高风险审批；浏览、读取和提取动作不绕过现有 Tool Registry。工具对直接输入的 URL 默认拒绝 localhost、内网 IP 字面量和常见内网域名。网页重定向和 DNS 解析可能改变最终目标；敏感网络环境还应配置 `browser_use_allowed_domains`，不能把此 URL 检查当成网络隔离。需要访问本地站点时，在 Workspace 配置中显式启用 `browser_use_allow_private_urls`。

## 配置

```json
{
  "browser_use_enabled": true,
  "browser_use_headless": false,
  "browser_use_timeout_seconds": 60,
  "browser_use_allowed_domains": [],
  "browser_use_allow_private_urls": false
}
```

浏览器会话随 Main Agent RuntimePool 保持，Gateway 关闭或 Runtime 被回收时自动释放。浏览器 Cookie 和 Profile 不写入 `~/.yy` 全局目录。

## 版权

browser-use 以 MIT License 发布；YYAgent 仅作为依赖调用其公开 Python API，源码和版权归原项目作者所有。
