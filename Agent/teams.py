"""Agent Team 的持久任务 DAG 与一次性交付邮箱。

``TeamStore`` 只封装 SQLite 中的团队状态，不负责运行模型。任务调度与并发上限由
``AgentRuntime.run_team`` 实现。任务依赖在写入前校验；领取使用带状态条件的原子
``UPDATE``，使多个 teammate 并发竞争同一任务时至多一个成功。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from .storage import StateStore
from .types import utc_now


class TeamStore:
    """在共享 ``StateStore`` 中管理团队任务和成员消息。"""

    # 作为外部输入校验和 UI 展示的统一状态词表；数据库本身目前不设 CHECK 约束。
    VALID_STATUS = {"pending", "in_progress", "completed", "failed"}

    def __init__(self, store: StateStore) -> None:
        """绑定状态库；多个 ``TeamStore`` 实例可安全共享同一数据库文件。"""

        self.store = store

    def create_team(self, name: str | None = None) -> str:
        """返回用户指定名称或生成短团队 ID。

        团队本身没有独立数据表；只要后续任务和消息使用相同 ``team_id`` 即构成团队。
        """

        return name or f"team-{uuid4().hex[:8]}"

    def add_task(
        self,
        team_id: str,
        title: str,
        description: str = "",
        dependencies: list[str] | None = None,
    ) -> str:
        """添加待处理任务，并拒绝指向当前团队未知任务的依赖。"""

        task_id, now = uuid4().hex[:8], utc_now()
        dependencies = dependencies or []
        existing = {row["id"] for row in self.list_tasks(team_id)}
        missing = set(dependencies) - existing
        if missing:
            raise ValueError(f"未知依赖任务：{', '.join(sorted(missing))}")
        self.store.execute(
            "INSERT INTO team_tasks VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task_id, team_id, title, description, "pending", None, json.dumps(dependencies), None, now, now),
        )
        return task_id

    def list_tasks(self, team_id: str) -> list[dict[str, Any]]:
        """按创建时间返回团队任务，并反序列化依赖 ID 列表。"""

        rows = self.store.query("SELECT * FROM team_tasks WHERE team_id=? ORDER BY created_at", (team_id,))
        for row in rows:
            row["dependencies"] = json.loads(row.pop("dependencies_json"))
        return rows

    def claim(self, team_id: str, task_id: str, owner: str) -> bool:
        """在依赖完成后尝试原子领取任务。

        前置读取用于验证 DAG 就绪条件，最终 ``UPDATE ... status='pending'`` 才是并发
        正确性的保证；若另一执行者抢先领取，``rowcount`` 为零并返回 ``False``。
        """

        tasks = {row["id"]: row for row in self.list_tasks(team_id)}
        task = tasks.get(task_id)
        if not task or task["status"] != "pending":
            return False
        if any(tasks.get(dependency, {}).get("status") != "completed" for dependency in task["dependencies"]):
            return False
        with self.store.connection() as database:
            cursor = database.execute(
                "UPDATE team_tasks SET status='in_progress',owner=?,updated_at=? "
                "WHERE id=? AND team_id=? AND status='pending'",
                (owner, utc_now(), task_id, team_id),
            )
            return cursor.rowcount == 1

    def complete(self, team_id: str, task_id: str, result: str, *, failed: bool = False) -> None:
        """保存任务结果，并切换到终态 ``completed`` 或 ``failed``。"""

        self.store.execute(
            "UPDATE team_tasks SET status=?,result=?,updated_at=? WHERE id=? AND team_id=?",
            ("failed" if failed else "completed", result, utc_now(), task_id, team_id),
        )

    def send(self, team_id: str, sender: str, recipient: str, message: str) -> None:
        """写入未交付消息；拒绝空参与方、空正文及超大消息。"""

        # 100 KB 限制避免模型意外把完整上下文或二进制数据灌入共享邮箱。
        if not sender or not recipient or not message.strip() or len(message) > 100_000:
            raise ValueError("无效团队消息")
        self.store.execute(
            "INSERT INTO mailboxes(team_id,sender,recipient,message,delivered,created_at) VALUES(?,?,?,?,0,?)",
            (team_id, sender, recipient, message, utc_now()),
        )

    def receive(self, team_id: str, recipient: str) -> list[dict[str, Any]]:
        """读取全部未交付消息，并在同一次调用中将其标记为已交付。

        返回的行仍保留读取前的 ``delivered=0`` 值，表示本批消息是首次投递；重复调用
        不会再次返回它们。占位符由行数生成，具体 ID 仍通过参数绑定以避免 SQL 注入。
        """

        rows = self.store.query(
            "SELECT * FROM mailboxes WHERE team_id=? AND recipient=? AND delivered=0 ORDER BY seq",
            (team_id, recipient),
        )
        if rows:
            ids = [str(row["seq"]) for row in rows]
            placeholders = ",".join("?" for _ in ids)
            self.store.execute(f"UPDATE mailboxes SET delivered=1 WHERE seq IN ({placeholders})", tuple(ids))
        return rows


__all__ = ["TeamStore"]
