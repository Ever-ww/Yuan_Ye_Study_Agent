# Runtime 插件目录

`catalog.json` 声明插件的名称、用途、资源目录、适用任务、依赖及是否允许在 Web「能力 → Plugins」中启停。Gateway 的文件监测会验证变更并发布不可变 Generation；已开始的任务继续使用旧版本，新任务使用新版本。增加可执行能力或修改执行代码时仍走现有 Reload 审批。

声明字段：`plugin_id`、`display_name`、`description`、`root`（`source`/`agent`）、`path`（相对于对应根目录）、`kind`（`tool`/`skill`/`extension`/`observer` 等）、`profiles`、可选 `requires_plugins` 与 `toggleable`。不存在的资源目录不会发布；路径逃逸、重复 ID、缺失依赖和冲突均拒绝发布。

独立 Skill 插件可以放在 `runtime-plugins/<plugin_id>/runtime-resources/interactive/skills/<skill_name>/SKILL.md`（`cron`、`dream` 同理），再为其单独添加一条声明。快照会把它挂载到对应任务的 `skills/<skill_name>`，因此停用时不会被其他插件的目录扫描重新加载；重复挂载位置会拒绝发布。

独立可执行 Extension 可以放在 `runtime-plugins/<plugin_id>/extension/hook/<stage>/<name>.py`，声明 `kind: "extension"` 和 `profiles: ["interactive"]`。它会挂载到现有 Extension Loader，并继续经过源码检查、Manifest 权限与 Reload 审批；启用开关本身不授予新权限。

这不是任意 Python 包加载器：新增 Tool 仍需遵守现有 `tools` 注册约定，Extension 仍需通过 Hook Manifest 和持久授权；仅添加一条目录声明不会绕过这些检查。核心适配器、内置工具和基础技能不可通过 Web 关闭。启停设置保存在 Agent 全局控制目录的 `.yy/runtime-plugins/settings.json`，不是源码或 Workspace 文件。
