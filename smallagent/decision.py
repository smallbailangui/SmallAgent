"""决策层：校验模型动作并决定是否执行。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Decision:
    """一次动作决策的结果。"""

    allowed: bool
    reason: str = ""
    risk: str = "low"


class DecisionPolicy:
    """最小决策策略，后续可扩展权限、风险分级和人工确认。"""

    def decide(self, action: dict[str, Any]) -> Decision:
        action_type = action.get("type")
        if action_type == "final":
            return Decision(True, "最终回答可进入完成度检查", "low")
        if action_type != "tool":
            return Decision(False, "未知动作类型", "high")

        tool = str(action.get("tool", ""))
        if not tool:
            return Decision(False, "缺少工具名", "high")
        if not isinstance(action.get("args", {}), dict):
            return Decision(False, "工具参数必须是对象", "medium")
        return Decision(True, f"允许执行工具 {tool}", self._classify_tool_risk(tool))

    def _classify_tool_risk(self, tool: str) -> str:
        if tool in {"get_cwd", "list_files", "read_file"}:
            return "low"
        if tool in {"write_file", "replace_text"}:
            return "medium"
        if tool == "run_shell":
            return "high"
        return "medium"
