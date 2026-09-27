# YYAgent 插件、能力与 Workspace 隔离设计

## 1. 目标

YYAgent 的插件源码和能力描述由源码仓库统一维护；Workspace 不保存 Skill、Tool 或 Hook 的源码副本，只保存当前项目选择了哪些能力。

目标边界：

```text
源码仓库
→ 插件的唯一事实来源

用户目录 ~/.yy
→ 全局控制、Gateway、Workspace Manifest、Backup
→ 外部下载 Skill 的隔离、审核和隔离失败处理

Workspace/.yy
→ 当前项目自己的数据
→ 能力启用声明和版本锁定
```

不同 Workspace 可以使用同一份全局插件源码，但每个 Workspace 独立决定是否启用以及使用哪些配置。

## 2. 概念定义

### 2.1 Plugin

Plugin 是拥有唯一 ID、版本、描述、依赖、权限和生命周期的扩展包。

插件可以提供一种或多种 Runtime Contribution，例如：

```text
Tool
Skill
Hook / Extension
Observer
Stable Prompt
Dynamic Context
```

Contribution 是插件提供的具体能力，不等同于插件本身。

### 2.2 Runtime Generation

Runtime Generation 是对当前插件源码、版本、依赖和配置做出的不可变快照。

它保证：

- 同一个 Turn 内插件不会中途改变；
- 正在运行的任务继续使用原版本；
- 新 Turn 可以使用新版本；
- 出错时可以回滚到旧版本；
- 审计记录可以准确定位实际执行的插件版本。

## 3. 三层目录职责

### 3.1 源码仓库：唯一事实来源

```text
D:\Ever_workspace\Yuan_Ye_Study_Agent
├─ runtime-plugins/
│  └─ catalog.json
├─ skills/
├─ tools/
├─ extension/
├─ observer_plugins/
└─ harness-evolution/
```

这里保存：

- 插件源码；
- Skill.md 和引用资源；
- Tool 实现；
- Hook/Extension 实现；
- 插件描述和依赖；
- 默认启用状态；
- 适用运行模式；
- 版本和兼容性信息。

源码目录是插件的正式来源，不能把 Workspace 中的副本当作 canonical source。

### 3.2 用户目录 `C:\Users\<user>\.yy`

用户目录仍然是全局控制目录，但不保存 Workspace 的业务数据，也不作为插件正式信息库。

```text
~/.yy/
├─ workspaces.json
├─ gateway/
├─ backups/
├─ runtime-plugins/
│  ├─ generations/
│  ├─ approvals/
│  └─ quarantine/
└─ skill-downloads/
   ├─ incoming/
   ├─ audit/
   └─ rejected/
```

用途：

- Gateway 全局状态；
- Workspace 注册表；
- 全局 Backup Manifest；
- Runtime Generation 快照；
- 外部下载 Skill 的临时隔离；
- 静态审核、隔离和拒绝记录；
- 插件加载失败后的 quarantine。

下载的 Skill 在未审核前不得直接进入 Runtime，也不得直接写入 Workspace。

用户目录中的隔离副本不是插件的正式来源。只有审核通过并正式纳入源码插件目录后，才允许被 Workspace 声明使用。

### 3.3 Workspace `.yy`

```text
D:\Ever_workspace\Research\.yy
├─ capabilities.json
├─ capabilities.lock
├─ profile/
├─ papers/
├─ references/
├─ notes/
├─ dream/
└─ code/
```

Workspace `.yy` 保存当前项目的数据和能力选择，但不保存以下内容：

```text
Skill 源码
Tool 源码
Hook 源码
插件完整安装包
```

## 4. Workspace 能力声明

### 4.1 `capabilities.json`

该文件表达用户希望当前 Workspace 使用哪些插件：

```json
{
  "version": 1,
  "plugins": {
    "builtin.tools": {
      "enabled": true,
      "profiles": ["interactive", "cron", "subagent"]
    },
    "builtin.skills": {
      "enabled": true,
      "profiles": ["interactive", "cron", "dream"]
    },
    "search-summary-paper": {
      "enabled": true,
      "profiles": ["interactive", "read"]
    },
    "runtime.skills.dream": {
      "enabled": false
    }
  }
}
```

这里只保存：

- 插件 ID；
- 是否启用；
- 当前 Workspace 允许的运行模式；
- Workspace 专属配置引用；
- 用户授权状态。

### 4.2 `capabilities.lock`

锁定实际使用的源码版本和 Hash：

```json
{
  "version": 1,
  "plugins": {
    "search-summary-paper": {
      "version": "1.0.0",
      "source_hash": "sha256:...",
      "generation_id": "..."
    }
  }
}
```

`capabilities.json` 表示用户选择，`capabilities.lock` 表示 Runtime 实际使用的版本。

## 5. 能力解析流程

每次创建或恢复 Runtime 时，按照以下顺序解析：

```text
源码 catalog.json
        ↓
全局可用插件与版本
        ↓
当前 Workspace capabilities.json
        ↓
依赖闭包检查
        ↓
运行模式过滤
        ↓
权限、审批和安全策略
        ↓
capabilities.lock
        ↓
不可变 Runtime Generation
```

插件只有同时满足以下条件才会加载：

```text
源码存在
+ 声明合法
+ Workspace 已启用
+ 依赖满足
+ 当前运行模式允许
+ 权限已通过
```

如果源码不存在、版本不匹配或依赖缺失，前端显示“不可用”，不能静默降级成已启用状态。

## 6. 默认能力策略

新建 Workspace 只默认开启核心能力：

```text
builtin.tools
builtin.skills
builtin.observer
builtin.prompts
```

默认关闭：

```text
联网能力
Browser Use
外部服务插件
实验性 Tool
Extension Hook
Dream 专属附加能力
Cron 专属附加能力
```

默认关闭的插件必须由用户在 Web 或 TUI 中主动开启，并经过依赖和权限检查。

## 7. Skill 设计

Skill 是当前模型与目标设计差距最大的部分。

### 目标行为

```text
源码 skills/
→ 唯一正式来源

用户目录 ~/.yy/skill-downloads/
→ 外部下载隔离和安全审核

Workspace/.yy/capabilities.json
→ 只声明是否使用
```

Workspace 不再保存：

```text
.yy/skills/installed
.yy/skills/review
.yy/skills/audit
.yy/skills/backups
```

Skill 会话仍然需要绑定当前 Workspace 的能力快照，但读取内容来自全局源码 Runtime Generation，不从 Workspace 复制目录读取。

## 8. Tool 设计

Tool 源码和 Runtime Generation 全局统一：

```text
源码 tools/
源码 harness-evolution/runtime/tools/
        ↓
用户目录 ~/.yy/runtime-plugins/generations/
        ↓
当前 Workspace Runtime 使用
```

Workspace 不保存 Tool 源码。

Tool 是否对某个 Workspace 可用，由 `capabilities.json`、运行模式、权限和 Tool 风险等级共同决定。

## 9. Hook / Extension 设计

Hook/Extension 源码同样不放进 Workspace：

```text
源码 extension/
        ↓
全局 Runtime Generation
        ↓
当前 Workspace HookRegistry
```

Hook 执行时可以获得当前 Workspace 的：

- Workspace 路径映射；
- Session；
- Memory；
- ToolContext；
- 权限和审计上下文。

核心安全 Hook 始终启用，不允许 Workspace 关闭。

普通 Extension Hook 可以由 Workspace 独立开启或关闭。

## 10. 生命周期

### 全局安装

```text
下载或放入插件源码
→ 用户目录隔离区
→ 静态审核
→ 依赖和权限检查
→ 正式纳入源码插件目录
```

### Workspace 启用

```text
修改 Workspace/.yy/capabilities.json
→ 校验依赖
→ 生成新的 capabilities.lock
→ 发布新的 Runtime Generation
```

### Reload

```text
当前 Turn：继续使用旧 Generation
新 Turn：使用新 Generation
新 Session：直接使用新 Generation
```

不得中途替换正在执行的模型、Tool、Skill 或 Hook。

### 失败隔离

如果插件加载、初始化或 Smoke Test 失败：

```text
新 Generation 不激活
→ 插件标记为 quarantine
→ 旧 Generation 继续运行
→ Web/TUI 展示失败原因和恢复操作
```

## 11. Web/TUI 表现

能力页面按当前 Workspace 展示：

```text
已安装可用
当前 Workspace 已启用
当前 Workspace 未启用
缺少源码
依赖缺失
待审核
已隔离
核心组件
```

每项显示：

- 名称；
- 描述；
- 版本；
- 来源；
- 适用模式；
- 依赖；
- 权限；
- 当前 Workspace 是否启用；
- 是否需要 Reload；
- 是否需要审批。

“未安装”表示全局源码或资源不存在；“未启用”表示全局存在但当前 Workspace 没有选择它。

## 12. Backup 规则

全局 Backup Manifest 负责发现所有 Workspace：

```text
~/.yy/workspaces.json
        ↓
备份每个 Workspace 的 .yy 数据和项目数据
```

Workspace Backup 包含：

- Profile；
- Papers；
- References；
- Notes；
- Dream；
- Code 状态；
- `capabilities.json`；
- `capabilities.lock`。

Workspace Backup 不重复保存全局插件源码。全局插件源码、审核记录和 Runtime Generation 由全局 Backup 单独管理。

恢复单个 Workspace 时：

```text
恢复 Workspace 数据
→ 恢复 capabilities 声明
→ 检查全局插件源码是否存在
→ 重新解析并生成 Runtime Generation
```

如果插件版本缺失，Workspace 可以恢复，但对应能力必须显示为不可用，不能阻塞整个 Workspace 启动。

## 13. 从当前实现迁移

1. 读取每个 Workspace 当前的 `.yy/skills/installed`、`index.json` 和审核报告。
2. 按内容 Hash 去重，确认正式 Skill 来源位于源码插件目录。
3. 把每个 Workspace 当前启用的 Skill 转写为 `capabilities.json`。
4. 把当前版本和 Hash 写入 `capabilities.lock`。
5. 将审核记录和下载隔离记录迁移到用户目录的全局隔离区。
6. 校验 Workspace 能从源码 Runtime Generation 读取 Skill。
7. 通过校验后，才删除 Workspace 中的 Skill 源码副本。
8. 源码目录只保留源码和插件资源，不再作为 Workspace 运行目录。

迁移必须有版本号和幂等标记，重复执行不得覆盖用户修改或产生第二份 Skill 数据。

## 14. 验收标准

- 新 Workspace 不会创建 `skills/installed` 或 Tool 源码副本；
- Skill、Tool、Hook 的正式实现都来自源码 Runtime Generation；
- Workspace 只保存能力声明和版本锁定；
- 两个 Workspace 可以独立启用和关闭同一插件；
- 核心安全 Hook 不能被关闭；
- 外部下载 Skill 在审核前不能进入 Runtime；
- 插件加载失败不会破坏旧 Generation；
- Gateway 重启后可以恢复全局插件和 Workspace 声明；
- Workspace Backup 不重复打包全局插件源码；
- 全局 Backup 能恢复插件来源和 Runtime Generation；
- Web 和 TUI 显示的插件状态一致；
- 源码目录不再产生 `.yy` Workspace 运行数据；
- 当前 Turn 不因 Reload 被中途替换；
- 新 Turn 可以按声明使用新插件版本。

## 15. 当前实现差距

当前代码已经具备：

- 声明式 Runtime Plugin Catalog；
- 全局 Runtime Generation；
- 插件依赖和安全校验；
- Tool、Skill、Hook、Extension 的按 Profile 选择；
- Reload、审批、回滚和旧 Generation 保留；
- Workspace 级 Tool/Skill 能力开关的一部分。

当前仍需调整：

- Skill 不应继续把安装副本、审核副本和备份放在 Workspace；
- Runtime Plugin 启停配置不应只使用全局 `settings.json`；
- Optional Hook/Extension 需要支持 Workspace 级启停声明；
- Workspace 需要统一的 `capabilities.json` 和 `capabilities.lock`；
- 源码目录必须与 Workspace 运行目录彻底分离。

