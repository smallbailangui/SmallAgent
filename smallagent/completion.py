"""完成度检查：在最终回答前做轻量 harness 校验。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


MODIFYING_TOOLS = {"write_file", "replace_text"}
VERIFICATION_TOOLS = {"read_file", "list_files", "run_shell"}
CONTEXT_TOOLS = {"get_cwd", "list_files", "read_file", "run_shell"}
MUTATION_KEYWORDS = (
    "创建",
    "写",
    "新增",
    "添加",
    "修改",
    "修复",
    "实现",
    "完善",
    "更新",
    "create",
    "write",
    "add",
    "modify",
    "fix",
    "implement",
    "update",
)
TEST_KEYWORDS = (
    "测试",
    "检查",
    "验证",
    "test",
    "check",
    "verify",
    "lint",
)


@dataclass
class AcceptanceCriterion:
    """一条可由工具轨迹检查的任务验收标准。"""

    key: str
    description: str


@dataclass
class CompletionCheck:
    """一次最终回答是否可接受的判断。"""

    accepted: bool
    reasons: list[str] = field(default_factory=list)
    criteria_status: list[str] = field(default_factory=list)

    def to_prompt(self) -> str:
        status = "通过" if self.accepted else "未通过"
        lines = [f"完成度检查：{status}。"]
        if self.criteria_status:
            lines.append("验收标准：")
            lines.extend(f"- {item}" for item in self.criteria_status)
        if self.reasons:
            lines.append("阻塞原因：")
            lines.extend(f"- {reason}" for reason in self.reasons)
        return "\n".join(lines)


class CompletionHarness:
    """根据工具轨迹判断 agent 是否有足够证据结束任务。"""

    def __init__(self) -> None:
        self.tool_results: list[dict[str, Any]] = []
        self.criteria: list[AcceptanceCriterion] = []

    def start_task(self, task: str) -> None:
        self.tool_results = []
        self.criteria = self._build_acceptance_criteria(task)

    def record_tool_result(self, result: dict[str, Any]) -> None:
        self.tool_results.append(dict(result))

    def to_prompt(self) -> str:
        if not self.criteria:
            return "验收标准：暂无。"
        lines = ["验收标准："]
        for index, criterion in enumerate(self.criteria, 1):
            lines.append(f"{index}. {criterion.description}")
        return "\n".join(lines)

    def evaluate(self, final_message: str) -> CompletionCheck:
        reasons: list[str] = []
        criteria_status: list[str] = []
        if not final_message.strip():
            reasons.append("最终回答不能为空，需要说明完成了什么和如何验证。")

        if not self.tool_results:
            reasons.append("缺少工具观察结果，需先收集工作区证据再结束。")

        if self.tool_results and not bool(self.tool_results[-1].get("ok", False)):
            tool = str(self.tool_results[-1].get("tool", "unknown"))
            reasons.append(f"最近一次工具 {tool} 失败，需先修复或收集新的成功证据。")

        last_mutation_index = self._last_successful_mutation_index()
        if last_mutation_index is not None and not self._has_verification_after(last_mutation_index):
            reasons.append("检测到文件修改后缺少验证证据，需读取关键文件或运行检查命令。")

        for criterion in self.criteria:
            met = self._criterion_met(criterion)
            mark = "已满足" if met else "未满足"
            criteria_status.append(f"{mark}：{criterion.description}")
            if not met:
                reasons.append(f"验收标准未满足：{criterion.description}")

        return CompletionCheck(accepted=not reasons, reasons=reasons, criteria_status=criteria_status)

    def _build_acceptance_criteria(self, task: str) -> list[AcceptanceCriterion]:
        normalized = task.lower()
        criteria = [
            AcceptanceCriterion("context", "已通过工具收集任务相关工作区上下文。"),
        ]
        expects_mutation = any(keyword in normalized for keyword in MUTATION_KEYWORDS)
        expects_test = any(keyword in normalized for keyword in TEST_KEYWORDS)

        if expects_mutation:
            criteria.append(AcceptanceCriterion("mutation", "如果任务要求改动，已有成功的文件修改证据。"))
            criteria.append(AcceptanceCriterion("verification", "文件修改后已有成功的复查或验证证据。"))
        elif expects_test:
            criteria.append(AcceptanceCriterion("verification", "已有成功的检查或验证证据。"))

        if expects_test:
            criteria.append(AcceptanceCriterion("run_shell", "任务提到测试或检查时，已成功运行 shell 验证命令。"))

        return criteria

    def _criterion_met(self, criterion: AcceptanceCriterion) -> bool:
        if criterion.key == "context":
            return self._has_successful_tool(CONTEXT_TOOLS)
        if criterion.key == "mutation":
            return self._last_successful_mutation_index() is not None
        if criterion.key == "verification":
            last_mutation_index = self._last_successful_mutation_index()
            if last_mutation_index is None:
                return self._has_successful_tool(VERIFICATION_TOOLS)
            return self._has_verification_after(last_mutation_index)
        if criterion.key == "run_shell":
            return self._has_successful_tool({"run_shell"})
        return False

    def _has_successful_tool(self, tools: set[str]) -> bool:
        return any(result.get("tool") in tools and bool(result.get("ok", False)) for result in self.tool_results)

    def _last_successful_mutation_index(self) -> int | None:
        for index in range(len(self.tool_results) - 1, -1, -1):
            result = self.tool_results[index]
            if result.get("tool") in MODIFYING_TOOLS and bool(result.get("ok", False)):
                return index
        return None

    def _has_verification_after(self, mutation_index: int) -> bool:
        for result in self.tool_results[mutation_index + 1 :]:
            if result.get("tool") in VERIFICATION_TOOLS and bool(result.get("ok", False)):
                return True
        return False
