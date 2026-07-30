# Hook Extension 开发指南

`extension/hook/` 是 Yuan Ye Agent 的全局源码扩展目录。它不随普通
workspace 切换；Gateway 启动时只扫描一次，因此新增或修改扩展后必须重启
Gateway 才会生效。

## 文件结构

十个 Hook 阶段各自拥有一个目录。每个目录可以包含任意数量、名称不同的
Python 文件，每个文件只实现一项清晰能力。请使用
`session_audit.py`、`collect_tool_metrics.py` 这类描述性名称，不要持续把新逻辑
堆入初始示例文件。

同一能力需要跨阶段时，在不同目录中使用相同文件名即可，例如
`trace_start/runtime_metrics.py` 与 `trace_end/runtime_metrics.py`。

文件名必须匹配 `[a-z][a-z0-9_]{0,63}.py`，Loader 只扫描阶段目录的直接
`*.py` 子文件，不扫描下级目录，也不接受符号链接。

## 模块契约

每个文件必须导出：

```python
from Agent.extensions import ExtensionContext
from Agent.hook import HookEvent

EXTENSION_NAME = "session-audit"
PRIORITY = 0

async def handle(event: HookEvent, context: ExtensionContext) -> None:
    ...
```

- `EXTENSION_NAME` 在同一阶段内唯一。
- `PRIORITY` 必须为 `-50..50`；数值越小越先执行，同优先级按文件名排序。
- `handle` 必须是参数名为 `event, context` 的异步函数。
- 导入、契约或执行失败会中止当前运行，并报告阶段和文件路径，不会静默忽略。

`ExtensionContext` 仅提供 Agent Home、Yuan Ye 源码根目录、当前 workspace、
扩展状态目录、Provider/model 名称和 Sandbox 开关。它不会提供 API Key、
数据库连接、UI 或模型客户端。

## 十个阶段

| 阶段 | 触发时机 | 常用 `event.data` |
|---|---|---|
| `trace_start` | Session 本次打开后 | `task`, `new_session` |
| `trace_end` | Session/Runtime 关闭前 | `error` |
| `turn_start` | 用户任务进入上下文前 | `task`, `config` |
| `turn_end` | 最终回答持久化或任务失败后 | `task`, `final`, `error`, `completed`, `cancelled` |
| `model_before` | 组合请求且真正调用模型前 | `task`, `messages`, `tools`, `model` |
| `model_during` | Provider 请求开始时 | `task`, `messages`, `tools`, `model` |
| `model_after` | Provider 成功或失败后 | `task`, `reply`, `error`, `model_call` |
| `tool_before` | 工具参数校验和执行前 | `name`, `arguments`, `tool_call_id` |
| `tool_during` | 工具实现开始执行时 | `name`, `arguments`, `tool_call_id` |
| `tool_after` | 工具成功、失败或取消后 | `name`, `arguments`, `result`, `error` |

只有 `model_before` 的 `messages`/`tools` 与 `tool_before` 的 `name`/`arguments`
属于既有核心流程允许改写的字段。改写工具参数后仍会重新经过 Schema 校验和
危险操作审批。其他字段默认只读；扩展不得绕过工具注册器、审批、Sandbox 或
文件锁直接执行危险操作。

## 测试与 `/code`

扩展测试放入 `tests/extensions/`。每项能力应使用独立、可描述行为的测试文件，
并覆盖正常、边界和异常路径。

CLI 输入 `/code` 会在 Agent Home 的隔离 Git worktree 中开启持续 Coding
会话。每一条 `Code >` 需求必须同时产生扩展代码和唯一测试文件，控制器会独立
检查修改路径、模块契约并运行专项测试、Extension 全套测试和项目回归测试。
测试通过后才会在临时分支提交；输入 `/exit` 后仅在主源码仓库仍干净且 HEAD
未变化时执行 fast-forward 合并。失败或越界修改不会被合并。
