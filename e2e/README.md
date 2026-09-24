# Browser E2E

这些测试使用真实 Chromium、Gateway 和 Web 前端，不使用伪造 DOM 或 API。

## 运行

先启动 Gateway/Web，获取 `serve-ui` 输出的一次性 URL：

```powershell
uv run python run.py serve-ui
```

然后在另一个终端进入 `e2e` 目录，安装依赖并执行：

```powershell
cd e2e
npm install
npm run install:browsers
$env:YY_E2E_URL = "http://127.0.0.1:8765/?bootstrap=..."
npm test
```

`YY_E2E_URL` 只用于第一次浏览器认证；global setup 会保存 Cookie/CSRF 状态，
同一轮测试中的其他用例复用该状态。

测试包含 Agent、Read、Write、Note、Code、Operations、Capabilities 路由冒烟，
命令面板、侧边栏收回/展开、键盘、Tooltip、Reduced Motion 和四种窄屏尺寸。
