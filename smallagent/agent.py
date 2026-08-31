"""智能体循环与模型响应解析。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from .completion import CompletionCheck, CompletionHarness
from .decision import DecisionPolicy
from .memory import ShortTermMemory
from .perception import Perception
from .planning import Plan, Planner
from .prompts import SYSTEM_PROMPT
from .tools import ToolRegistry


class ChatClient(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> str:
        """返回下一条模型消息。"""


@dataclass
class AgentConfig:
    max_steps: int = 12


@dataclass
class AgentResult:
    final_message: str
    steps: int
    history: list[dict[str, str]] = field(default_factory=list)
    completion_check: CompletionCheck | None = None


class ResponseParseError(ValueError):
    """模型输出无法解析为智能体动作时抛出。"""


def parse_agent_response(text: str) -> dict[str, Any]:
    """解析模型输出的 JSON；同时兼容 Markdown 代码块。"""
    candidate = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL | re.IGNORECASE)
    if fence_match:
        candidate = fence_match.group(1).strip()

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
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
    ) -> None:
        self.client = client
        self.tools = tools
        self.config = config or AgentConfig()
        self.planner = planner or Planner()
        self.memory = memory or ShortTermMemory()
        self.decision_policy = decision_policy or DecisionPolicy()
        self.completion_harness = completion_harness or CompletionHarness()

    def run(self, task: str) -> AgentResult:
        self.completion_harness.start_task(task, self.tools.workspace)
        perception = Perception(task, self.tools.workspace)
        plan = self.planner.create_initial_plan(task)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._state_prompt(perception, plan)},
            {"role": "user", "content": task},
        ]

        for step in range(1, self.config.max_steps + 1):
            perception.observe_step(step)
            raw_response = self.client.complete(messages)
            messages.append({"role": "assistant", "content": raw_response})

            try:
                response = parse_agent_response(raw_response)
            except ResponseParseError as exc:
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

            decision = self.decision_policy.decide(response)
            if not decision.allowed:
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

            if response["type"] == "final":
                message = str(response.get("message", "")).strip()
                completion = self.completion_harness.evaluate(message)
                if not completion.accepted:
                    # final 是模型提出的“我完成了”。如果只差推荐验证命令，
                    # agent 可以自己补一次验收，而不是把同一个请求再丢回模型。
                    plan = self._try_auto_verification(perception, plan, messages, completion)
                    completion = self.completion_harness.evaluate(message)
                if not completion.accepted:
                    messages.append(
                        {
                            "role": "user",
                            "content": self._completion_feedback(completion.to_prompt()),
                        }
                    )
                    continue
                return AgentResult(message or "Done.", step, messages, completion)

            tool_name = str(response.get("tool", ""))
            args = response.get("args", {})
            if not isinstance(args, dict):
                result = {
                    "ok": False,
                    "tool": tool_name,
                    "output": "",
                    "error": "tool args must be a JSON object",
                }
            else:
                result = self.tools.run(tool_name, args)

            perception.observe_tool_result(result)
            self.memory.remember_tool_result(result)
            self.completion_harness.record_tool_call(tool_name, args if isinstance(args, dict) else {}, result)
            plan = self.planner.update_after_tool(plan, result["tool"], result["ok"])
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
            messages.append({"role": "user", "content": self._state_prompt(perception, plan)})

        completion = self.completion_harness.evaluate("")
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

    def _state_prompt(self, perception: Perception, plan: Plan) -> str:
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
        result = self.tools.run("run_recommended_verification", args)
        perception.observe_tool_result(result)
        self.memory.remember_tool_result(result)
        self.completion_harness.record_tool_call("run_recommended_verification", args, result)
        plan = self.planner.update_after_tool(plan, result["tool"], result["ok"])
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
