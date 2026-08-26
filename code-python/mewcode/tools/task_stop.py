# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from mewcode.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from mewcode.agents.task_manager import TaskManager


class TaskStopParams(BaseModel):
    task_id: str = (
        "ID 或名称 of the background task to stop. Stopped agents keep their "
        "context and can be continued later via SendMessage."
    )


class TaskStopTool(Tool):
    name = "TaskStop"
    description = (
        "Stop a running background task / worker.\n\n"
        "Use this when a worker was sent in the wrong direction or its work is "
        "no longer needed. The stopped worker is NOT destroyed — it keeps its "
        "loaded context and can be continued later via SendMessage with its "
        "agent ID or name."
    )
    params_model = TaskStopParams
    category = "command"
    is_concurrency_safe = False

    def __init__(self, task_manager: TaskManager) -> None:
        self._task_manager = task_manager

    def _resolve(self, key: str):
        bg = self._task_manager.get(key)
        if bg is not None:
            return bg
        # 按名称匹配（Agent 工具返回的是 task ID，但用户/模型可能只知道名称）
        for t in self._task_manager.list_tasks():
            if t.name == key:
                return t
        return None

    async def execute(self, params: BaseModel) -> ToolResult:
        p: TaskStopParams = params  # type: ignore[assignment]

        bg = self._resolve(p.task_id)
        if bg is None:
            return ToolResult(
                output=f"Error: no running task found for '{p.task_id}'",
                is_error=True,
            )
        if bg.status != "running":
            return ToolResult(
                output=f"Task '{bg.name}' ({bg.id}) is not running (status: {bg.status}). "
                "Nothing to stop."
            )

        cancelled = self._task_manager.cancel(bg.id)
        if not cancelled:
            return ToolResult(
                output=f"Error: failed to cancel task '{bg.name}' ({bg.id})",
                is_error=True,
            )

        return ToolResult(
            output=(
                f"Task '{bg.name}' ({bg.id}) stopped.\n"
                f"Agent ID: {bg.agent.agent_id}\n"
                "The worker kept its context — you can continue it later via "
                "SendMessage with that agent ID."
            )
        )
