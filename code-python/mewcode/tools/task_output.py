# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from pydantic import BaseModel

from mewcode.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from mewcode.agents.task_manager import TaskManager


class TaskOutputParams(BaseModel):
    task_id: str = "ID or name of the background task"


class TaskOutputTool(Tool):
    name = "TaskOutput"
    description = (
        "Check the current status and (partial) output of a background task.\n\n"
        "Returns the task's status (running/completed/failed/cancelled), "
        "elapsed time, token usage and its latest result text. Use this "
        "instead of polling Agent — background tasks notify you on completion."
    )
    params_model = TaskOutputParams
    category = "read"
    is_concurrency_safe = True

    def __init__(self, task_manager: TaskManager) -> None:
        self._task_manager = task_manager

    def _resolve(self, key: str):
        bg = self._task_manager.get(key)
        if bg is not None:
            return bg
        for t in self._task_manager.list_tasks():
            if t.name == key:
                return t
        return None

    async def execute(self, params: BaseModel) -> ToolResult:
        p: TaskOutputParams = params  # type: ignore[assignment]

        bg = self._resolve(p.task_id)
        if bg is None:
            running = [
                f"  {t.id} ({t.name}) [{t.status}]" for t in self._task_manager.list_tasks()
            ]
            listing = "\n".join(running) if running else "  (no tasks)"
            return ToolResult(
                output=(
                    f"Error: no task found for '{p.task_id}'.\n"
                    f"Known tasks:\n{listing}"
                ),
                is_error=True,
            )

        elapsed = (bg.end_time or time.monotonic()) - bg.start_time
        lines = [
            f"Task {bg.id} ({bg.name}): {bg.status}",
            f"  Elapsed: {elapsed:.0f}s",
            f"  Tokens: {bg.agent.total_input_tokens} in / "
            f"{bg.agent.total_output_tokens} out",
        ]
        if bg.result:
            preview = bg.result[:2000]
            lines.append(f"\nResult:\n{preview}")
            if len(bg.result) > 2000:
                lines.append(f"… ({len(bg.result) - 2000} more chars)")
        else:
            lines.append("\n(no output yet — task is still running)")

        return ToolResult(output="\n".join(lines))
