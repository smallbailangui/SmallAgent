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
class Evidence:
    """一条从工具结果提炼出的验收证据。"""

    tool: str
    ok: bool
    tags: set[str] = field(default_factory=set)
    summary: str = ""

    def to_prompt(self) -> str:
        status = "成功" if self.ok else "失败"
        tags = ",".join(sorted(self.tags)) if self.tags else "uncategorized"
        if self.summary:
            return f"{self.tool} {status} [{tags}]：{self.summary}"
        return f"{self.tool} {status} [{tags}]"

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "tags": sorted(self.tags),
            "summary": self.summary,
        }


@dataclass
class CompletionCheck:
    """一次最终回答是否可接受的判断。"""

    accepted: bool
    reasons: list[str] = field(default_factory=list)
    criteria_status: list[str] = field(default_factory=list)
    evidence_summary: list[str] = field(default_factory=list)

    def to_prompt(self) -> str:
        status = "通过" if self.accepted else "未通过"
        lines = [f"完成度检查：{status}。"]
        if self.criteria_status:
            lines.append("验收标准：")
            lines.extend(f"- {item}" for item in self.criteria_status)
        if self.evidence_summary:
            lines.append("最近证据：")
            lines.extend(f"- {item}" for item in self.evidence_summary)
        if self.reasons:
            lines.append("阻塞原因：")
            lines.extend(f"- {reason}" for reason in self.reasons)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "criteria_status": list(self.criteria_status),
            "evidence_summary": list(self.evidence_summary),
        }


class CompletionHarness:
    """根据工具轨迹判断 agent 是否有足够证据结束任务。"""

    def __init__(self) -> None:
        self.tool_results: list[dict[str, Any]] = []
        self.evidence: list[Evidence] = []
        self.criteria: list[AcceptanceCriterion] = []

    def start_task(self, task: str) -> None:
        self.tool_results = []
        self.evidence = []
        self.criteria = self._build_acceptance_criteria(task)

    def record_tool_result(self, result: dict[str, Any]) -> None:
        copied = dict(result)
        self.tool_results.append(copied)
        self.evidence.append(self._evidence_from_result(copied))

    def to_prompt(self) -> str:
        if not self.criteria:
            return "验收标准：暂无。"
        lines = ["验收标准："]
        for index, criterion in enumerate(self.criteria, 1):
            lines.append(f"{index}. {criterion.description}")
        if self.evidence:
            lines.append("最近证据：")
            for evidence in self.evidence[-5:]:
                lines.append(f"- {evidence.to_prompt()}")
        return "\n".join(lines)

    def evaluate(self, final_message: str) -> CompletionCheck:
        reasons: list[str] = []
        criteria_status: list[str] = []
        evidence_summary = [evidence.to_prompt() for evidence in self.evidence[-5:]]
        if not final_message.strip():
            reasons.append("最终回答不能为空，需要说明完成了什么和如何验证。")

        if not self.evidence:
            reasons.append("缺少工具观察结果，需先收集工作区证据再结束。")

        if self.evidence and not self.evidence[-1].ok:
            reasons.append(f"最近一次工具 {self.evidence[-1].tool} 失败，需先修复或收集新的成功证据。")

        last_mutation_index = self._last_successful_mutation_index()
        if last_mutation_index is not None and not self._has_verification_after(last_mutation_index):
            reasons.append("检测到文件修改后缺少验证证据，需读取关键文件或运行检查命令。")

        for criterion in self.criteria:
            met = self._criterion_met(criterion)
            mark = "已满足" if met else "未满足"
            criteria_status.append(f"{mark}：{criterion.description}")
            if not met:
                reasons.append(f"验收标准未满足：{criterion.description}")

        return CompletionCheck(
            accepted=not reasons,
            reasons=reasons,
            criteria_status=criteria_status,
            evidence_summary=evidence_summary,
        )

    def _evidence_from_result(self, result: dict[str, Any]) -> Evidence:
        tool = str(result.get("tool", "unknown"))
        ok = bool(result.get("ok", False))
        output = str(result.get("output", "") or result.get("error", ""))
        tags = self._tags_for_tool(tool, ok)
        return Evidence(tool=tool, ok=ok, tags=tags, summary=output[:160])

    def _tags_for_tool(self, tool: str, ok: bool) -> set[str]:
        if not ok:
            return {"failure"}
        tags: set[str] = set()
        if tool in CONTEXT_TOOLS:
            tags.add("context")
        if tool in MODIFYING_TOOLS:
            tags.add("mutation")
        if tool in VERIFICATION_TOOLS:
            tags.add("verification")
        if tool == "run_shell":
            tags.add("shell")
        return tags or {"tool"}

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
            return self._has_successful_evidence("context")
        if criterion.key == "mutation":
            return self._last_successful_mutation_index() is not None
        if criterion.key == "verification":
            last_mutation_index = self._last_successful_mutation_index()
            if last_mutation_index is None:
                return self._has_successful_evidence("verification")
            return self._has_verification_after(last_mutation_index)
        if criterion.key == "run_shell":
            return self._has_successful_evidence("shell")
        return False

    def _has_successful_evidence(self, tag: str) -> bool:
        return any(evidence.ok and tag in evidence.tags for evidence in self.evidence)

    def _last_successful_mutation_index(self) -> int | None:
        for index in range(len(self.evidence) - 1, -1, -1):
            evidence = self.evidence[index]
            if evidence.ok and "mutation" in evidence.tags:
                return index
        return None

    def _has_verification_after(self, mutation_index: int) -> bool:
        for evidence in self.evidence[mutation_index + 1 :]:
            if evidence.ok and "verification" in evidence.tags:
                return True
        return False
