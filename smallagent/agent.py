"""智能体循环与模型响应解析。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .completion import CompletionCheck, CompletionHarness
from .decision import Decision, DecisionPolicy
from .memory import ShortTermMemory
from .perception import Perception
from .planning import Plan, Planner
from .prompts import SYSTEM_PROMPT
from .tools import ToolRegistry


class ChatClient(Protocol):
    """模型客户端协议。

    只要对象实现 complete(messages) -> str，就可以被 CodingAgent 使用；
    测试里会用假的 client 替代真实网络请求。
    """

    def complete(self, messages: list[dict[str, str]]) -> str:
        """返回下一条模型消息。"""


@dataclass
class AgentConfig:
    """agent 运行配置。"""

    max_steps: int = 12


@dataclass
class AgentResult:
    """agent 一次运行结束后的完整结果。"""

    final_message: str
    steps: int
    history: list[dict[str, str]] = field(default_factory=list)
    completion_check: CompletionCheck | None = None


class ResponseParseError(ValueError):
    """模型输出无法解析为智能体动作时抛出。"""


ApprovalCallback = Callable[[dict[str, Any], Decision], bool]
TraceCallback = Callable[[dict[str, Any]], None]


def parse_agent_response(text: str) -> dict[str, Any]:
    """解析模型输出的 JSON；同时兼容 Markdown 代码块。"""
    candidate = text.strip()
    # 有些模型会把 JSON 包在 ```json 代码块里，这里先剥掉外层代码块。
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL | re.IGNORECASE)
    if fence_match:
        candidate = fence_match.group(1).strip()

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        # 兜底处理：如果模型前后多写了一点文字，尝试截取第一个 JSON 对象。
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            raise ResponseParseError("expected a JSON object")
        try:
            data = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ResponseParseError(f"expected a JSON object: {exc}") from exc

    if not isinstance(data, dict):
        raise ResponseParseError("top-level response must be a JSON object")
    if data.get("type") not in {"tool", "final"}:
        raise ResponseParseError('response type must be "tool" or "final"')
    return data


class CodingAgent:
    """一个带本地工具和显式停止条件的小型编程智能体。"""

    def __init__(
        self,
        client: ChatClient,
        tools: ToolRegistry,
        config: AgentConfig | None = None,
        planner: Planner | None = None,
        memory: ShortTermMemory | None = None,
        decision_policy: DecisionPolicy | None = None,
        completion_harness: CompletionHarness | None = None,
        approval_callback: ApprovalCallback | None = None,
        trace_callback: TraceCallback | None = None,
    ) -> None:
        self.client = client
        self.tools = tools
        self.config = config or AgentConfig()
        self.planner = planner or Planner()
        self.memory = memory or ShortTermMemory()
        self.decision_policy = decision_policy or DecisionPolicy()
        self.completion_harness = completion_harness or CompletionHarness()
        self.approval_callback = approval_callback
        self.trace_callback = trace_callback

    def run(self, task: str, session_context: str = "") -> AgentResult:
        """执行一个用户任务。

        主循环的职责是把“模型文本”变成“可执行动作”，再把工具结果变回模型可读的
        OBSERVATION。模型决定下一步，程序负责执行、记录和验收。

        session_context 来自交互式终端的会话摘要；单次任务模式默认不传。
        它只作为额外上下文提示模型，不参与 completion harness 的确定性验收。
        """
        # 完成度 harness 在任务开始时生成验收标准，并拍一份工作区初始快照。
        self.completion_harness.start_task(task, self.tools.workspace)
        # Memory 是任务内 working memory；复用同一个 CodingAgent 实例时不能带入上一任务。
        self.memory.clear()
        # perception/plan/memory/completion 会被合并进状态提示，帮助模型知道当前进度。
        perception = Perception(task, self.tools.workspace)
        plan = self.planner.create_initial_plan(task)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._state_prompt(perception, plan)},
        ]
        if session_context.strip():
            # 交互式终端里的上一轮任务摘要放在用户任务前，帮助模型理解“继续/它”等指代。
            messages.append({"role": "user", "content": session_context})
        messages.append({"role": "user", "content": task})

        for step in range(1, self.config.max_steps + 1):
            perception.observe_step(step)
            self._emit_trace(self._state_trace_event(step, perception, plan))
            # 让模型基于当前 messages 选择下一步：调用工具或返回 final。
            raw_response = self.client.complete(messages)
            messages.append({"role": "assistant", "content": raw_response})

            try:
                response = parse_agent_response(raw_response)
            except ResponseParseError as exc:
                self._emit_trace({"event": "parse_error", "step": step, "error": str(exc)})
                messages.append(
                    {
                        "role": "user",
                        "content": self._observation(
                            False,
                            "parse_error",
                            "",
                            f"{exc}. Reply with only the required JSON object.",
                        ),
                    }
                )
                continue

            # 决策层先检查动作格式和风险，再允许真正执行工具。
            decision = self.decision_policy.decide(response)
            if not decision.allowed:
                self._emit_trace(
                    {
                        "event": "decision_blocked",
                        "step": step,
                        "reason": decision.reason,
                        "risk": decision.risk,
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": self._observation(
                            False,
                            "decision",
                            "",
                            f"{decision.reason}；风险等级：{decision.risk}",
                        ),
                    }
                )
                continue
            self._emit_trace(
                {
                    "event": "model_action",
                    "step": step,
                    "action_type": response["type"],
                    "tool": response.get("tool"),
                    "reason": response.get("reason", ""),
                    "decision": decision.reason,
                }
            )

            if response["type"] == "final":
                message = str(response.get("message", "")).strip()
                # final 不会被直接相信，必须先通过 completion harness。
                completion = self.completion_harness.evaluate(message)
                self._emit_final_check(step, completion)
                if not completion.accepted:
                    # final 是模型提出的“我完成了”。如果只差推荐验证命令，
                    # agent 可以自己补一次验收，而不是把同一个请求再丢回模型。
                    evidence_count = len(self.completion_harness.evidence)
                    plan = self._try_auto_verification(perception, plan, messages, completion, step)
                    if len(self.completion_harness.evidence) > evidence_count:
                        completion = self.completion_harness.evaluate(message)
                        self._emit_final_check(step, completion)
                if not completion.accepted:
                    messages.append(
                        {
                            "role": "user",
                            "content": self._completion_feedback(completion.to_prompt()),
                        }
                    )
                    continue
                return AgentResult(message or "Done.", step, messages, completion)

            # 走到这里说明模型选择了工具调用，tool_name/args 会交给 ToolRegistry。
            tool_name = str(response.get("tool", ""))
            args = response.get("args", {})
            if not self._approved(response, decision):
                self._emit_trace(
                    {
                        "event": "approval",
                        "step": step,
                        "tool": tool_name,
                        "allowed": False,
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": self._observation(
                            False,
                            "approval",
                            "",
                            f"用户拒绝执行高风险工具 {tool_name}；请选择更安全的方式继续。",
                        ),
                    }
                )
                continue
            if decision.risk == "high" and self.approval_callback is not None:
                self._emit_trace(
                    {
                        "event": "approval",
                        "step": step,
                        "tool": tool_name,
                        "allowed": True,
                    }
                )
            if not isinstance(args, dict):
                result = {
                    "ok": False,
                    "tool": tool_name,
                    "output": "",
                    "error": "tool args must be a JSON object",
                }
            else:
                result = self.tools.run(tool_name, args)

            # 更新当前观察状态
            perception.observe_tool_result(result)
            # 保存短期运行记忆
            self.memory.remember_tool_result(result, args if isinstance(args, dict) else {})
            # 保存验收证据
            self.completion_harness.record_tool_call(tool_name, args if isinstance(args, dict) else {}, result)
            plan = self.planner.update_after_tool(plan, result["tool"], result["ok"])
            self._emit_trace(
                {
                    "event": "tool_result",
                    "step": step,
                    "tool": result["tool"],
                    "ok": result["ok"],
                    "summary": result.get("output", ""),
                    "error": result.get("error", ""),
                    "perception": self._perception_summary(perception),
                    "memory": self._memory_summary(),
                    "plan": self._plan_summary(plan),
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": self._observation(
                        result["ok"],
                        result["tool"],
                        result.get("output", ""),
                        result.get("error", ""),
                        result.get("metadata"),
                    ),
                }
            )
            # 每次工具调用后都追加最新状态，让模型下一轮看到感知、计划、记忆和验收标准。
            messages.append({"role": "user", "content": self._state_prompt(perception, plan)})

        # 超过最大步数仍未 final 时，返回停止信息，同时附带当前完成度检查结果。
        completion = self.completion_harness.evaluate("")
        self._emit_final_check(self.config.max_steps, completion)
        return AgentResult(
            f"Stopped after {self.config.max_steps} steps without a final answer.",
            self.config.max_steps,
            messages,
            completion,
        )

    @staticmethod
    def _observation(
        ok: bool,
        tool: str,
        output: str,
        error: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """把工具结果包装成模型下一轮可读的 OBSERVATION。"""
        payload = {"ok": ok, "tool": tool, "output": output, "error": error}
        if metadata is not None:
            payload["metadata"] = metadata
        return "OBSERVATION:\n" + json.dumps(payload, ensure_ascii=False)

    def _approved(self, action: dict[str, Any], decision: Decision) -> bool:
        """在高风险工具执行前调用外部确认回调。

        非交互模式默认没有 approval_callback，所以保持原来的自动执行行为。
        交互终端会注入回调，让 run_shell 等 high 风险工具先经过用户确认。
        """
        if action.get("type") != "tool":
            return True
        if decision.risk != "high" or self.approval_callback is None:
            return True
        return self.approval_callback(action, decision)

    def _state_prompt(self, perception: Perception, plan: Plan) -> str:
        """合并四类运行状态，作为下一轮模型输入。

        这是架构的关键拼接点：感知告诉模型刚发生什么，计划告诉模型下一步方向，
        记忆保留最近事实，completion harness 告诉模型还缺哪些验收证据。
        """
        return "\n\n".join(
            [
                perception.state.to_prompt(),
                plan.to_prompt(),
                self.memory.to_prompt(),
                self.completion_harness.to_prompt(),
            ]
        )

    @staticmethod
    def _completion_feedback(message: str) -> str:
        return (
            "SELF_CHECK:\n"
            + message
            + "\n请继续调用工具补足证据，修复问题后再返回 final。"
        )

    def _try_auto_verification(
        self,
        perception: Perception,
        plan: Plan,
        messages: list[dict[str, str]],
        completion: CompletionCheck,
        step: int,
    ) -> Plan:
        """在 final 前自动补一次推荐验证。

        这个自动化非常保守：只有缺失项完全属于 run_shell/recommended_verification 时才运行。
        代码修改、上下文不足、最近工具失败或空 final 都交还给模型继续处理。
        """
        command = self.completion_harness.next_recommended_verification()
        if command is None:
            return plan
        if any(reason.startswith(("最终回答不能为空", "最近一次工具")) for reason in completion.reasons):
            return plan
        missing = completion.missing_criteria_keys()
        if not missing or not missing <= {"run_shell", "recommended_verification"}:
            return plan

        args = {"index": 1, "timeout": 120}
        self._emit_trace(
            {
                "event": "auto_verification",
                "step": step,
                "command": command.command,
            }
        )
        result = self.tools.run("run_recommended_verification", args)
        perception.observe_tool_result(result)
        self.memory.remember_tool_result(result, args)
        self.completion_harness.record_tool_call("run_recommended_verification", args, result)
        plan = self.planner.update_after_tool(plan, result["tool"], result["ok"])
        self._emit_trace(
            {
                "event": "tool_result",
                "step": step,
                "tool": result["tool"],
                "ok": result["ok"],
                "summary": result.get("output", ""),
                "error": result.get("error", ""),
                "perception": self._perception_summary(perception),
                "memory": self._memory_summary(),
                "plan": self._plan_summary(plan),
            }
        )
        messages.append(
            {
                "role": "user",
                "content": self._observation(
                    result["ok"],
                    result["tool"],
                    result.get("output", ""),
                    result.get("error", ""),
                    result.get("metadata"),
                ),
            }
        )
        messages.append(
            {
                "role": "user",
                "content": "AUTO_VERIFY:\n" + command.to_prompt() + "\n\n" + self._state_prompt(perception, plan),
            }
        )
        return plan

    def _emit_final_check(self, step: int, completion: CompletionCheck) -> None:
        self._emit_trace(
            {
                "event": "final_check",
                "step": step,
                "accepted": completion.accepted,
                "reasons": completion.reasons,
            }
        )

    def _emit_trace(self, event: dict[str, Any]) -> None:
        """向外部报告公开可展示的执行轨迹；trace 失败不影响 agent 本体运行。"""
        if self.trace_callback is None:
            return
        try:
            self.trace_callback(event)
        except Exception:
            return

    def _state_trace_event(self, step: int, perception: Perception, plan: Plan) -> dict[str, Any]:
        return {
            "event": "step_started",
            "step": step,
            "perception": self._perception_summary(perception),
            "plan": self._plan_summary(plan),
            "memory": self._memory_summary(),
            "acceptance": self._acceptance_summary(),
        }

    @staticmethod
    def _perception_summary(perception: Perception) -> str:
        state = perception.state
        if not state.last_tool:
            return f"任务：{state.task}"
        status = "成功" if state.last_ok else "失败"
        preview = state.last_output_preview.replace("\n", " ")[:120]
        return f"上次观察：{state.last_tool} {status}；{preview}"

    @staticmethod
    def _plan_summary(plan: Plan) -> str:
        if not plan.steps:
            return f"目标：{plan.goal}"
        return " -> ".join(plan.steps[-3:])

    def _memory_summary(self) -> str:
        if not self.memory.items:
            return "暂无短期记忆"
        latest = self.memory.items[-1].content.replace("\n", " ")[:140]
        return f"已记录 {len(self.memory.items)} 条；最近：{latest}"

    def _acceptance_summary(self) -> str:
        count = len(self.completion_harness.criteria)
        if count == 0:
            return "暂无验收标准"
        descriptions = [criterion.description for criterion in self.completion_harness.criteria[:3]]
        return f"{count} 条验收标准；" + "；".join(descriptions)
