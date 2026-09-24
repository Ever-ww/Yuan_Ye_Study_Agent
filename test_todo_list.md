# YYAgent P0/P1 测试清单

> 每项必须有可运行测试和验证结果；未执行的外部服务测试不得标记为完成。

## P0：核心可用性

- [x] 审查现有 Python、TUI、Gateway、Backup、Workspace、Note、Paper 测试边界。
- [x] Gateway → Runtime → Tool → Event Store → Session JSONL 本地真实链路。
- [x] Gateway → Runtime → Tool 的真实流式 SSE 链路。
- [x] 流式输出取消、断线、重连、事件去重和顺序恢复后端回归。
- [x] Workspace A/B 数据、Profile、Paper、Note、Session 隔离。
- [x] 仅恢复 Workspace A 时不会修改 Workspace B。
- [x] Gateway 重启后恢复 Session、Run、Approval、Event Cursor。
- [x] 启动时 `backup_pending` 不阻塞 Gateway、Workspace 或 Agent。
- [x] Bootstrap、认证、CSRF 和 Gateway 重连后端回归。
- [x] 浏览器 Agent 流式回答、Tool Approval、Observer、刷新恢复 E2E。
- [x] Browser 最小启动 Smoke Test。
- [x] Tauri compile/launch smoke：`desktop/smoke.ps1` 已验证编译和开发窗口存活。

## P1：主要用户功能

- [x] Read/PDF 后端导入、临时附件、Summary 状态和论文回收站回归。
- [x] LLM 翻译请求单元测试：只发送选中文本并关闭 reasoning。
- [x] Gateway 前端事件合并、终态文本和 replay cursor 单元测试。
- [x] Read/PDF 连续滚动、文本选择、翻译侧栏和流式翻译组件测试。
- [x] PDF 选区视觉状态与翻译数据状态分离。
- [x] Summary Markdown 表格、PDF Worker 错误和论文回收站。
- [x] NoteStore 新建、编辑、自动保存、冲突、回收站、恢复和外部变更后端回归。
- [x] Note 页面组件和浏览器交互测试。
- [x] Code Session API 生命周期和 Harness Runtime 隔离后端回归。
- [x] Code/Dream/Tool 跨模式 Observer 上下文和浏览器交互测试。
- [x] Code finalize、冲突、abort 和 Gateway 重启恢复后端回归。
- [x] API 错误状态：结构化 409、非 JSON 503、认证/CSRF 后端回归。
- [x] 逻辑路径、真实路径脱敏和错误响应安全后端回归。
- [x] 关键页面键盘操作、焦点、Tooltip、Reduced Motion 和窄屏布局。

## 验证门槛

- [x] 默认 pytest（排除 external）通过。
- [x] unittest 全量通过。
- [x] 前端 Vitest、typecheck、production build 通过。
- [x] Python compileall 和 `git diff --check` 通过。
- [x] 外部 Provider 测试仅在显式凭证下单独运行。

## 当前阻塞

- 无浏览器测试阻塞。Playwright Chromium 已安装，真实本机 Gateway 工作台 E2E 已执行通过。
- 外部 Provider 测试没有凭证时只允许保持 skipped，不得标记为已验证。

## 本轮验证记录

- `pytest -q`（默认测试集，排除 external）：667 passed, 5 skipped, 1 deselected。
- P0/P1 关键后端回归（Gateway 本地链路、Backup、Paper、Note、Workspace、Runtime）：116 passed。
- `python -m unittest`：430 passed, 3 skipped。
- `npm --prefix ui test -- --run`：5 个测试文件、19 tests passed。
- `npm --prefix ui run build`：production build passed；仅有大 chunk warning。
- `python -m compileall`、`git diff --check`：通过；后者只有 ReaderPage 的 CRLF 转换提示，没有 whitespace error。
- `pytest -q -m external`：无凭证时按设计 skipped，不访问真实 Provider。
- `powershell -ExecutionPolicy Bypass -File desktop/smoke.ps1`：Tauri compile smoke 通过；设置 `YY_TAURI_RUN_SMOKE=1` 后开发窗口存活检查通过。
- `YY_E2E_URL=... npm --prefix e2e test`：20 个 Playwright Chromium 测试全部通过（路由/启动、键盘焦点、Tooltip、Reduced Motion、窄屏、Agent 流式/Approval/Observer/刷新、Read PDF 选区/流式翻译、Note 自动保存、Code/Dream 隔离交互）。
