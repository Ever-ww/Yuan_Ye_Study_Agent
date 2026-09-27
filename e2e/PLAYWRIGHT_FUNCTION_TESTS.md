# YYAgent Playwright 功能测试清单

这份清单是浏览器端功能测试的单一入口。每一项对应一个 Playwright 用例或一组可复用的测试断言；出现回归时，先补充对应项，再修复实现。

## 运行前提

在项目根目录启动 Gateway 和 Web：

```powershell
uv run python run.py serve-ui
```

在另一个终端运行：

```powershell
cd e2e
npm install
npm run install:browsers
$env:YY_E2E_URL = "http://127.0.0.1:8765/?bootstrap=..."
npm test
```

只运行 Note 与 Write 边界测试：

```powershell
npx playwright test note-workspace.spec.ts
```

不启动 Gateway 也可以检查所有测试是否能被编译和发现：

```powershell
npx playwright test --list
```

没有设置 `YY_E2E_URL` 时，功能测试会安全跳过，不会误把“没有测试环境”报告成产品失败。

Note/Write 的 API mock 测试也可以脱离 Gateway 运行。先启动前端开发服务器，再执行：

```powershell
$env:YY_E2E_MOCK = "1"
$env:YY_E2E_BASE_URL = "http://127.0.0.1:4173"
npx playwright test note-workspace.spec.ts
```

## 测试文件与覆盖范围

| 编号 | 测试项 | Playwright 文件 | 验收重点 |
| --- | --- | --- | --- |
| P0-01 | 应用壳与认证 | `accessibility.spec.ts` | Gateway bootstrap、页面可加载、无 fatal error |
| P0-02 | 响应式布局 | `accessibility.spec.ts` | 320/768/1024/1440 宽度无横向溢出 |
| P0-03 | 键盘与 Reduced Motion | `accessibility.spec.ts` | Tab、焦点可见、Reduced Motion 不产生强制动画 |
| P1-01 | Agent 新建会话 | `workbench.spec.ts` | 项目、Session、输入区和发送流程 |
| P1-02 | Agent 流式过程 | `feature-workflows.spec.ts` | Turn、工具批次、思考过程和最终正文顺序一致 |
| P1-03 | Agent 恢复与断线 | `feature-workflows.spec.ts` | 刷新后恢复事件游标，不重复、不丢失 |
| P1-04 | Approval / Observer | `feature-workflows.spec.ts` | 审批决策条、Observer 完成态和错误态 |
| P2-01 | Inbox、Cron、Dream、Backup | `feature-workflows.spec.ts` | 列表、详情、读状态、控制操作二次确认 |
| P2-02 | Skill、Plugin、Extension | `feature-workflows.spec.ts` | 能力页、审批、reload/rollback 不绕过 Gateway |
| P2-03 | Code Session 隔离 | `feature-workflows.spec.ts` | Code 不复用 Agent session、路径不泄漏、冲突可恢复 |
| P3-01 | Note 默认知识库 | `note-workspace.spec.ts` | 进入 Note 自动创建幂等的研究文件夹 |
| P3-02 | Note Markdown 编辑 | `note-workspace.spec.ts` | Markdown 源码、文档模式、自动保存 |
| P3-03 | Markdown 表格渲染 | `note-workspace.spec.ts` | 表格在文档模式可读，源码模式可编辑 |
| P3-04 | Note 移动与回收站 | `note-workspace.spec.ts` | 文件夹移动、删除到回收站、恢复 |
| P3-05 | Note / Write 隔离 | `note-workspace.spec.ts` | Write 不显示 `YYWorkspace:\notes` 及其中的变更 |
| P3-06 | Read PDF | `feature-workflows.spec.ts` | 导入、PDF 连续滚动、选区菜单、翻译侧栏 |
| P4-01 | Workspace 文件操作 | `feature-workflows.spec.ts` | 树、编辑、ETag 冲突、Checkpoint 恢复 |
| P4-02 | LaTeX 工作流 | `feature-workflows.spec.ts` | 编译、诊断、取消、PDF 预览 |

## 本轮新增的 Note 检查

- [x] 默认目录集合固定且可重复初始化，不重复创建。
- [x] Note 的 Markdown 文件和附件留在当前 Workspace。
- [x] Write 树和变更列表过滤 `notes` 路径。
- [x] 新建文件夹后仍可以进入 Markdown 源码模式并触发 PATCH 保存。
- [x] 表格从 Markdown 源码渲染到文档模式。
- [x] 笔记移动到其他默认目录，再进入回收站并恢复。

## 失败处理约定

1. 先用 `npx playwright test <file> --debug` 定位浏览器交互问题。
2. 再用 `--trace on-first-retry` 生成 trace，确认是 UI、Gateway API 还是测试 mock 问题。
3. 产品行为有变化时同步更新本清单和 `test_todo_list.md`，不要只修改断言。
4. 修复后至少运行相关 Playwright 文件、前端 typecheck/build，以及对应 Python 单元测试。
