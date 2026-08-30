from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from smallagent.agent import AgentConfig, CodingAgent, parse_agent_response
from smallagent.tools import ToolRegistry


class FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0

    def complete(self, messages: list[dict[str, str]]) -> str:
        response = self.responses[self.calls]
        self.calls += 1
        return response


class AgentTests(unittest.TestCase):
    def test_parse_accepts_plain_and_fenced_json(self) -> None:
        self.assertEqual(parse_agent_response('{"type":"final","message":"ok"}')["type"], "final")
        self.assertEqual(
            parse_agent_response('```json\n{"type":"tool","tool":"get_cwd","args":{}}\n```')["tool"],
            "get_cwd",
        )

    def test_agent_runs_tool_then_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(
                [
                    '{"type":"tool","tool":"write_file","args":{"path":"hello.txt","content":"hi"}}',
                    '{"type":"final","message":"created hello.txt"}',
                ]
            )
            agent = CodingAgent(client, ToolRegistry(Path(tmp)), AgentConfig(max_steps=4))

            result = agent.run("create hello.txt")

            self.assertEqual(result.final_message, "created hello.txt")
            self.assertEqual((Path(tmp) / "hello.txt").read_text(encoding="utf-8"), "hi")
            self.assertEqual(result.steps, 2)


class ToolTests(unittest.TestCase):
    def test_paths_cannot_escape_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tools = ToolRegistry(Path(tmp))
            result = tools.run("read_file", {"path": "../secret.txt"})

            self.assertFalse(result["ok"])
            self.assertIn("escapes workspace", result["error"])

    def test_dangerous_shell_command_is_blocked_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tools = ToolRegistry(Path(tmp))
            result = tools.run("run_shell", {"command": "git reset --hard", "timeout": 5})

            self.assertFalse(result["ok"])
            self.assertIn("dangerous command blocked", result["error"])


if __name__ == "__main__":
    unittest.main()
