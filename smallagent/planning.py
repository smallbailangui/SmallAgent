"""规划层：维护可解释、可扩展的任务计划。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Plan:
    """当前任务的轻量计划。"""

    goal: str
    steps: list[str] = field(default_factory=list)

    def to_prompt(self) -> str:
        """转换成模型可阅读的计划摘要。"""
        if not self.steps:
            return f"当前计划：围绕目标“{self.goal}”先感知项目，再修改、验证、总结。"
        rendered = "\n".join(f"{index}. {step}" for index, step in enumerate(self.steps, 1))
        return f"当前计划：\n{rendered}"


class Planner:
    """生成和更新计划，后续可替换成更复杂的规划器。"""

    def create_initial_plan(self, task: str) -> Plan:
        """为新任务生成通用 coding-agent 流程。

        当前计划是启发式路线图，真正选择哪个工具仍由模型根据状态决定。
        """
        return Plan(
            goal=task,
            steps=[
                "理解用户任务和当前项目状态",
                "选择合适工具收集必要上下文",
                "执行最小必要修改",
                "运行验证命令",
                "总结结果和剩余风险",
            ],
        )

    def update_after_tool(self, plan: Plan, tool: str, ok: bool) -> Plan:
        """工具执行后追加一条计划进展，帮助模型理解刚才动作的结果。"""
        status = "成功" if ok else "失败，需要调整"
        plan.steps.append(f"工具 {tool} 执行{status}")
        return plan
