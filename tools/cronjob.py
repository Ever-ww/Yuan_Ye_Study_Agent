"""Agent 自然语言管理 Gateway Cron Job 的动态风险工具。"""

from __future__ import annotations

from typing import Any

from cron import (
    CronJobCreateRequest,
    CronJobEditRequest,
    CronSchedule,
    CronService,
    CronScheduleCalculator,
)

from tool.contracts import ToolContext, ToolRisk


class CronJobTool:
    name = "cronjob"
    description = (
        "管理当前项目的后台定时 Agent 任务。每次触发都由独立、无会话记忆的子 Agent 执行，"
        "因此 prompt 必须自包含；可校验/预览五段 Cron，也可创建、编辑、暂停、恢复、立即运行或删除任务。"
    )
    risk = "dynamic"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "list", "status", "validate", "preview", "create", "edit",
                    "pause", "resume", "run", "retry", "history", "remove",
                ],
            },
            "job_id": {"type": "string"},
            "name": {"type": "string"},
            "prompt": {
                "type": "string",
                "description": "无记忆 Cron 子 Agent 每次运行的完整、自包含任务说明",
            },
            "schedule": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["interval", "once", "cron"]},
                    "interval_seconds": {"type": "integer"},
                    "run_at": {"type": "string"},
                    "expression": {"type": "string"},
                    "timezone": {"type": "string"},
                },
                "required": ["kind"],
            },
            "count": {"type": "integer"},
            "preapproved_tools": {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
                "description": "无人值守运行中可使用的非只读工具；创建/修改时会随整个 Cron 请求进行高风险审批",
            },
        },
        "required": ["action"],
    }

    def __init__(self, service: CronService, project_id: str) -> None:
        self.service = service
        self.project_id = project_id

    def risk_for(self, arguments: dict[str, Any]) -> ToolRisk:
        return "read" if arguments["action"] in {"list", "status", "validate", "preview", "history"} else "high"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        del context
        action = arguments["action"]
        if action == "list":
            jobs = await self.service.list(self.project_id)
            return _json([item.model_dump(mode="json") for item in jobs])
        if action == "status":
            return (await self.service.status()).model_dump_json(indent=2)
        if action == "history":
            job_id = _required(arguments, "job_id")
            return _json([item.model_dump(mode="json") for item in await self.service.history(job_id)])
        if action in {"validate", "preview"}:
            schedule = _schedule(arguments)
            preview = self.service.preview(schedule, count=arguments.get("count", 5))
            return preview.model_dump_json(indent=2)
        if action == "create":
            job = await self.service.create(CronJobCreateRequest(
                project_id=self.project_id,
                name=_required(arguments, "name"),
                prompt=_required(arguments, "prompt"),
                schedule=_schedule(arguments),
                preapproved_tools=tuple(arguments.get("preapproved_tools", ())),
            ))
            return job.model_dump_json(indent=2)
        job_id = _required(arguments, "job_id")
        if action == "edit":
            job = await self.service.edit(job_id, CronJobEditRequest(
                name=arguments.get("name"),
                prompt=arguments.get("prompt"),
                schedule=_schedule(arguments) if arguments.get("schedule") else None,
                preapproved_tools=(
                    tuple(arguments["preapproved_tools"])
                    if "preapproved_tools" in arguments else None
                ),
            ))
        elif action == "pause":
            job = await self.service.pause(job_id)
        elif action == "resume":
            job = await self.service.resume(job_id)
        elif action == "run":
            job = await self.service.trigger(job_id)
        elif action == "retry":
            return (await self.service.retry(job_id)).model_dump_json(indent=2)
        elif action == "remove":
            job = await self.service.remove(job_id)
        else:  # Schema 已经阻止未知 action。
            raise ValueError(f"未知 Cron 操作：{action}")
        return job.model_dump_json(indent=2)


def _schedule(arguments: dict[str, Any]) -> CronSchedule:
    value = arguments.get("schedule")
    if not isinstance(value, dict):
        raise ValueError("当前操作需要 schedule")
    cleaned = {key: item for key, item in value.items() if item is not None}
    if cleaned.get("kind") == "cron" and "timezone" not in cleaned:
        cleaned["timezone"] = CronScheduleCalculator.local_timezone()
    return CronSchedule.model_validate(cleaned, strict=True)


def _required(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"当前操作需要 {key}")
    return value.strip()


def _json(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, indent=2)
