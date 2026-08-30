from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from smallagent.agent import AgentConfig, CodingAgent, parse_agent_response
from smallagent.config import load_dotenv
from smallagent.decision import DecisionPolicy
from smallagent.memory import ShortTermMemory
from smallagent.perception import Perception
from smallagent.planning import Planner
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
        self.assertEqual(
            parse_agent_response('Here is the action: {"type":"final","message":"done"}')["message"],
            "done",
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
            self.assertTrue(any("OBSERVATION" in item["content"] for item in result.history))

    def test_agent_injects_perception_plan_and_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(
                [
                    '{"type":"tool","tool":"get_cwd","args":{}}',
                    '{"type":"final","message":"完成"}',
                ]
            )
            memory = ShortTermMemory()
            agent = CodingAgent(
                client,
                ToolRegistry(Path(tmp)),
                AgentConfig(max_steps=3),
                planner=Planner(),
                memory=memory,
                decision_policy=DecisionPolicy(),
            )

            result = agent.run("查看目录")

            joined_history = "\n".join(item["content"] for item in result.history)
            self.assertIn("感知状态", joined_history)
            self.assertIn("当前计划", joined_history)
            self.assertIn("短期记忆", joined_history)
            self.assertEqual(len(memory.items), 1)

    def test_decision_rejects_invalid_tool_args(self) -> None:
        client = FakeClient(
            [
                '{"type":"tool","tool":"write_file","args":"bad"}',
                '{"type":"final","message":"已停止"}',
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            agent = CodingAgent(client, ToolRegistry(Path(tmp)), AgentConfig(max_steps=3))
            result = agent.run("错误动作")

            joined_history = "\n".join(item["content"] for item in result.history)
            self.assertIn("decision", joined_history)
            self.assertIn("工具参数必须是对象", joined_history)


class ArchitectureTests(unittest.TestCase):
    def test_perception_updates_from_tool_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            perception = Perception("任务", Path(tmp))
            perception.observe_step(2)
            perception.observe_tool_result({"tool": "read_file", "ok": True, "output": "内容", "error": ""})

            prompt = perception.state.to_prompt()

            self.assertIn("当前轮数：2", prompt)
            self.assertIn("read_file 成功", prompt)

    def test_planner_keeps_extendable_plan(self) -> None:
        planner = Planner()
        plan = planner.create_initial_plan("修复测试")
        planner.update_after_tool(plan, "run_shell", False)

        self.assertIn("运行验证命令", plan.to_prompt())
        self.assertIn("失败，需要调整", plan.to_prompt())


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


class ConfigTests(unittest.TestCase):
    def test_load_dotenv_reads_local_api_settings_without_overwriting_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "OPENAI_API_KEY=from-file\nSMALLAGENT_MODEL=demo-model\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"OPENAI_API_KEY": "from-env"}, clear=True):
                load_dotenv(env_file)

                self.assertEqual(__import__("os").environ["OPENAI_API_KEY"], "from-env")
                self.assertEqual(__import__("os").environ["SMALLAGENT_MODEL"], "demo-model")


if __name__ == "__main__":
    unittest.main()
