"""感知层：把任务、工作区和工具结果整理成结构化状态。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PerceptionState:
    """智能体当前能看到的任务状态。

    这里保存的是“压缩后的当前观察”，不是完整历史；完整历史在 AgentResult.history。
    """

    task: str
    workspace: str
    step: int = 0
    last_tool: str = ""
    last_ok: bool | None = None
    last_output_preview: str = ""
    signals: list[str] = field(default_factory=list)

    def to_prompt(self) -> str:
        """转换成给模型阅读的简短上下文。"""
        lines = [
            "感知状态：",
            f"- 任务：{self.task}",
            f"- 工作区：{self.workspace}",
            f"- 当前轮数：{self.step}",
        ]
        if self.last_tool:
            lines.append(f"- 上次工具：{self.last_tool}")
            lines.append(f"- 上次工具是否成功：{self.last_ok}")
        if self.last_output_preview:
            lines.append(f"- 上次输出摘要：{self.last_output_preview}")
        if self.signals:
            lines.append("- 关键线索：" + "；".join(self.signals[-5:]))
        return "\n".join(lines)


class Perception:
    """维护并更新智能体的感知状态。"""

    def __init__(self, task: str, workspace: Path) -> None:
        """初始化任务级感知状态。"""
        self.state = PerceptionState(task=task, workspace=str(workspace))

    def observe_step(self, step: int) -> None:
        """记录当前执行轮数，防止模型忘记已经迭代了多久。"""
        self.state.step = step

    def observe_tool_result(self, result: dict[str, Any]) -> None:
        """把一次工具结果压缩成下一轮模型可读的感知摘要。"""
        self.state.last_tool = str(result.get("tool", ""))
        self.state.last_ok = bool(result.get("ok", False))
        output = str(result.get("output", "") or result.get("error", ""))
        self.state.last_output_preview = output[:500]
        if self.state.last_ok:
            self.state.signals.append(f"{self.state.last_tool} 成功")
        else:
            self.state.signals.append(f"{self.state.last_tool} 失败")
