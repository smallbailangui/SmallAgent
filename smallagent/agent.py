"""Agent loop and response parsing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from .prompts import SYSTEM_PROMPT
from .tools import ToolRegistry


class ChatClient(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> str:
        """Return the next assistant message."""


@dataclass
class AgentConfig:
    max_steps: int = 12


@dataclass
class AgentResult:
    final_message: str
    steps: int
    history: list[dict[str, str]] = field(default_factory=list)


class ResponseParseError(ValueError):
    """Raised when the model response cannot be parsed as an agent command."""


def parse_agent_response(text: str) -> dict[str, Any]:
    """Parse the model response JSON, accepting a fenced JSON block as a convenience."""
    candidate = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL | re.IGNORECASE)
    if fence_match:
        candidate = fence_match.group(1).strip()

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ResponseParseError(f"expected a JSON object: {exc}") from exc

    if not isinstance(data, dict):
        raise ResponseParseError("top-level response must be a JSON object")
    if data.get("type") not in {"tool", "final"}:
        raise ResponseParseError('response type must be "tool" or "final"')
    return data


class CodingAgent:
    """A small ReAct-style coding agent with local tools and explicit stopping."""

    def __init__(
        self,
        client: ChatClient,
        tools: ToolRegistry,
        config: AgentConfig | None = None,
    ) -> None:
        self.client = client
        self.tools = tools
        self.config = config or AgentConfig()

    def run(self, task: str) -> AgentResult:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]

        for step in range(1, self.config.max_steps + 1):
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

            if response["type"] == "final":
                message = str(response.get("message", "")).strip()
                return AgentResult(message or "Done.", step, messages)

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

            messages.append(
                {
                    "role": "user",
                    "content": self._observation(
                        result["ok"],
                        result["tool"],
                        result.get("output", ""),
                        result.get("error", ""),
                    ),
                }
            )

        return AgentResult(
            f"Stopped after {self.config.max_steps} steps without a final answer.",
            self.config.max_steps,
            messages,
        )

    @staticmethod
    def _observation(ok: bool, tool: str, output: str, error: str) -> str:
        payload = {"ok": ok, "tool": tool, "output": output, "error": error}
        return "OBSERVATION:\n" + json.dumps(payload, ensure_ascii=False)
