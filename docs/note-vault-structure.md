# Note Workspace 知识库结构

Note 是 Workspace 内的本地 Markdown 知识库。Markdown 文件是唯一事实来源，SQLite 只保存可重建的索引、关系和回收站元数据。切换 Workspace 会切换整套 Note 数据，不读取其他项目的笔记。

## 默认目录

首次进入 Note 时，Gateway 通过幂等接口创建以下顶层目录：

```text
<workspace>/
├─ notes/
│  ├─ 00 Inbox/       # 临时想法、待整理内容
│  ├─ 10 Daily/       # 日记、每日工作记录
│  ├─ 20 Research/    # 研究问题、方法、结果
│  ├─ 30 Projects/    # 项目计划与交付记录
│  ├─ 40 Experiments/ # 实验设计、运行和复盘
│  ├─ 50 Literature/  # 论文阅读和文献卡片
│  ├─ 60 Meetings/    # 会议记录与决定
│  ├─ 70 Resources/   # 可复用资料、教程和参考
│  ├─ 90 Templates/   # 新笔记模板
│  └─ 99 Archive/     # 已完成但仍需保留的记录
├─ assets/
│  ├─ images/         # Note 图片资源
│  └─ attachments/    # Note 附件
└─ .yy/notes/
   ├─ notes.sqlite3   # 可重建索引，不是正文来源
   ├─ revisions/      # Markdown 版本快照
   └─ trash/          # 可恢复删除内容
```

目录只是默认起点，用户仍可在 Note 中建立嵌套文件夹。Write 模式只面向论文与 LaTeX 工作区，会隐藏 `notes/` 和 Note 变更；Note 模式才展示这些目录。

## Markdown 约定

每篇笔记是一个 `.md` 文件，标题由文件名和首个一级标题共同表达。保存时会写入最小 frontmatter：

```yaml
---
id: note-id
title: 实验记录
created: 2026-01-01T00:00:00Z
updated: 2026-01-01T00:00:00Z
parent_id: folder-id
tags: [research, experiment]
aliases: []
---
```

`[[笔记名称]]` 作为 WikiLink。SQLite 索引用于快速搜索、反向链接、版本历史和回收站；文件损坏或索引丢失时可以通过“重新扫描 Workspace”重建。

## Gateway 接口

```text
POST /api/v1/projects/{project_id}/notes/initialize-structure
GET  /api/v1/projects/{project_id}/notes
POST /api/v1/projects/{project_id}/notes
POST /api/v1/projects/{project_id}/notes/folders
PATCH /api/v1/projects/{project_id}/notes/{note_id}
POST /api/v1/projects/{project_id}/notes/{note_id}/move
DELETE /api/v1/projects/{project_id}/notes/{note_id}
```

`initialize-structure` 返回 `created` 和完整 `folders`，重复调用不会覆盖用户已有目录。删除先移动到 `.yy/notes/trash/`，恢复前不会永久删除文件。

## 对应测试

`e2e/note-workspace.spec.ts` 覆盖默认目录、Markdown 编辑与保存、表格渲染、移动/回收站恢复，以及 Note 与 Write 的边界。后端 `tests/test_notes.py` 覆盖默认目录初始化的幂等性。
