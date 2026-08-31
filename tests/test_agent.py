from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path

from smallagent.agent import AgentConfig, CodingAgent, parse_agent_response
from smallagent.cli import main
from smallagent.completion import CompletionHarness
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
                    '{"type":"tool","tool":"read_file","args":{"path":"hello.txt"}}',
                    '{"type":"final","message":"created hello.txt"}',
                ]
            )
            agent = CodingAgent(client, ToolRegistry(Path(tmp)), AgentConfig(max_steps=4))

            result = agent.run("create hello.txt")

            self.assertEqual(result.final_message, "created hello.txt")
            self.assertEqual((Path(tmp) / "hello.txt").read_text(encoding="utf-8"), "hi")
            self.assertEqual(result.steps, 4)
            self.assertIsNotNone(result.completion_check)
            self.assertTrue(result.completion_check.accepted)
            self.assertTrue(result.completion_check.evidence_summary)
            self.assertTrue(any("OBSERVATION" in item["content"] for item in result.history))
            self.assertTrue(any("SELF_CHECK" in item["content"] for item in result.history))

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
            self.assertIn("验收标准", joined_history)
            self.assertEqual(len(memory.items), 1)

    def test_decision_rejects_invalid_tool_args(self) -> None:
        client = FakeClient(
            [
                '{"type":"tool","tool":"write_file","args":"bad"}',
                '{"type":"final","message":"已停止"}',
                '{"type":"tool","tool":"get_cwd","args":{}}',
                '{"type":"final","message":"已停止"}',
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            agent = CodingAgent(client, ToolRegistry(Path(tmp)), AgentConfig(max_steps=4))
            result = agent.run("错误动作")

            joined_history = "\n".join(item["content"] for item in result.history)
            self.assertIn("decision", joined_history)
            self.assertIn("工具参数必须是对象", joined_history)
            self.assertIn("SELF_CHECK", joined_history)


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

    def test_decision_policy_classifies_tool_risk(self) -> None:
        policy = DecisionPolicy()

        self.assertEqual(policy.decide({"type": "tool", "tool": "read_file", "args": {}}).risk, "low")
        self.assertEqual(policy.decide({"type": "tool", "tool": "write_file", "args": {}}).risk, "medium")
        self.assertEqual(policy.decide({"type": "tool", "tool": "run_shell", "args": {}}).risk, "high")

    def test_completion_harness_requires_verification_after_modification(self) -> None:
        harness = CompletionHarness()

        harness.record_tool_result({"tool": "write_file", "ok": True, "output": "wrote app.py", "error": ""})
        failed_check = harness.evaluate("写入完成")

        self.assertFalse(failed_check.accepted)
        self.assertIn("缺少验证证据", failed_check.to_prompt())

        harness.record_tool_result({"tool": "read_file", "ok": True, "output": "print('ok')", "error": ""})
        passed_check = harness.evaluate("写入完成，已读取验证")

        self.assertTrue(passed_check.accepted)

    def test_completion_harness_rejects_final_after_failed_tool(self) -> None:
        harness = CompletionHarness()

        harness.record_tool_result({"tool": "run_shell", "ok": False, "output": "", "error": "exit 1"})
        check = harness.evaluate("完成")

        self.assertFalse(check.accepted)
        self.assertIn("最近一次工具 run_shell 失败", check.to_prompt())

    def test_completion_harness_builds_task_acceptance_criteria(self) -> None:
        harness = CompletionHarness()
        harness.start_task("修复测试并运行检查")

        self.assertIn("文件修改", harness.to_prompt())
        self.assertIn("shell 验证命令", harness.to_prompt())

        harness.record_tool_result({"tool": "read_file", "ok": True, "output": "old", "error": ""})
        partial_check = harness.evaluate("看起来完成")

        self.assertFalse(partial_check.accepted)
        self.assertIn("验收标准未满足", partial_check.to_prompt())

        harness.record_tool_result({"tool": "replace_text", "ok": True, "output": "replaced 1 occurrence(s)", "error": ""})
        harness.record_tool_result({"tool": "run_shell", "ok": True, "output": "OK", "error": ""})
        final_check = harness.evaluate("修复完成，测试通过")

        self.assertTrue(final_check.accepted)

    def test_completion_harness_extracts_structured_evidence(self) -> None:
        harness = CompletionHarness()
        harness.start_task("实现功能")

        harness.record_tool_result({"tool": "write_file", "ok": True, "output": "wrote main.py", "error": ""})
        harness.record_tool_result({"tool": "run_shell", "ok": True, "output": "Ran 1 test OK", "error": ""})

        self.assertEqual(harness.evidence[0].tool, "write_file")
        self.assertIn("mutation", harness.evidence[0].tags)
        self.assertIn("shell", harness.evidence[1].tags)
        self.assertIn("最近证据", harness.to_prompt())

        check = harness.evaluate("实现完成，验证通过")

        self.assertTrue(check.accepted)
        self.assertTrue(any("run_shell 成功" in item for item in check.evidence_summary))


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


class CliTests(unittest.TestCase):
    def test_cli_writes_completion_report_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.json"
            client = FakeClient(
                [
                    '{"type":"tool","tool":"get_cwd","args":{}}',
                    '{"type":"final","message":"已查看目录"}',
                ]
            )

            output = io.StringIO()
            with (
                patch("smallagent.cli.OpenAICompatibleClient.from_env", return_value=client),
                redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "--workspace",
                        tmp,
                        "--report-file",
                        str(report_path),
                        "查看目录",
                    ]
                )

            report = json.loads(report_path.read_text(encoding="utf-8"))

            self.assertEqual(exit_code, 0)
            self.assertEqual(report["final_message"], "已查看目录")
            self.assertTrue(report["completion_check"]["accepted"])
            self.assertTrue(report["completion_check"]["evidence_summary"])


if __name__ == "__main__":
    unittest.main()
