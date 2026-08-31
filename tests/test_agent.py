from __future__ import annotations

import io
import json
import subprocess
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
            self.assertEqual(result.completion_check.changed_files[0].path, "hello.txt")
            self.assertEqual(result.completion_check.changed_files[0].status, "added")
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

    def test_agent_auto_runs_recommended_verification_before_final(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "pyproject.toml").write_text("[project]\nname = \"demo\"\n", encoding="utf-8")
            (workspace / "tests").mkdir()
            (workspace / "tests" / "test_smoke.py").write_text(
                "import unittest\n\n"
                "class SmokeTest(unittest.TestCase):\n"
                "    def test_smoke(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            client = FakeClient(
                [
                    '{"type":"tool","tool":"write_file","args":{"path":"app.py","content":"print(1)\\n"}}',
                    '{"type":"tool","tool":"read_file","args":{"path":"app.py"}}',
                    '{"type":"final","message":"已创建 app.py 并复查"}',
                ]
            )
            agent = CodingAgent(client, ToolRegistry(workspace), AgentConfig(max_steps=4))

            result = agent.run("创建 app.py")

            self.assertEqual(result.final_message, "已创建 app.py 并复查")
            self.assertIsNotNone(result.completion_check)
            self.assertTrue(result.completion_check.accepted)
            self.assertTrue(any("AUTO_VERIFY" in item["content"] for item in result.history))
            self.assertTrue(
                any("recommended_verification" in item.tags for item in result.completion_check.evidence)
            )


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
        self.assertTrue(all(result.met for result in final_check.criteria_results))
        self.assertTrue(final_check.evidence)

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

        report = check.to_dict()

        self.assertEqual(report["evidence"][0]["index"], 1)
        self.assertIn("criteria_results", report)
        self.assertTrue(all(item["met"] for item in report["criteria_results"]))
        self.assertIn(1, report["criteria_results"][1]["evidence_indices"])

    def test_completion_harness_detects_workspace_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            original = workspace / "app.py"
            original.write_text("print('old')\n", encoding="utf-8")
            harness = CompletionHarness()
            harness.start_task("修改 app.py", workspace)

            original.write_text("print('new')\n", encoding="utf-8")
            (workspace / "created.py").write_text("print('created')\n", encoding="utf-8")
            changes = {item.path: item.status for item in harness.changed_files()}

            self.assertEqual(changes["app.py"], "modified")
            self.assertEqual(changes["created.py"], "added")

    def test_completion_harness_requires_recommended_verification_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "pyproject.toml").write_text("[project]\nname = \"demo\"\n", encoding="utf-8")
            (workspace / "tests").mkdir()
            target = workspace / "app.py"
            target.write_text("old\n", encoding="utf-8")
            harness = CompletionHarness()
            harness.start_task("修复测试", workspace)

            self.assertIn("python -m unittest discover -s tests -v", harness.to_prompt())

            harness.record_tool_result({"tool": "read_file", "ok": True, "output": "old", "error": ""})
            target.write_text("new\n", encoding="utf-8")
            harness.record_tool_result({"tool": "replace_text", "ok": True, "output": "replaced 1 occurrence(s)", "error": ""})
            harness.record_tool_call(
                "run_shell",
                {"command": "python -m unittest"},
                {"tool": "run_shell", "ok": True, "output": "OK", "error": ""},
            )
            incomplete = harness.evaluate("修复完成，测试通过")

            self.assertFalse(incomplete.accepted)
            self.assertIn("推荐的项目验证命令", incomplete.to_prompt())

            harness.record_tool_call(
                "run_shell",
                {"command": "python -m unittest discover -s tests -v"},
                {"tool": "run_shell", "ok": True, "output": "OK", "error": ""},
            )
            complete = harness.evaluate("修复完成，测试通过")

            self.assertTrue(complete.accepted)
            self.assertTrue(any("recommended_verification" in item.tags for item in complete.evidence))
            self.assertTrue(complete.changed_files)
            self.assertTrue(complete.verification_commands)
            self.assertEqual(complete.evidence[-1].details["command"], "python -m unittest discover -s tests -v")

    def test_completion_harness_rejects_unproven_test_pass_claim(self) -> None:
        harness = CompletionHarness()
        harness.start_task("查看目录")
        harness.record_tool_result({"tool": "get_cwd", "ok": True, "output": "workspace", "error": ""})

        check = harness.evaluate("测试通过")

        self.assertFalse(check.accepted)
        self.assertIn("缺少成功的 shell 验证证据", check.to_prompt())


class ToolTests(unittest.TestCase):
    def test_search_text_finds_matches_with_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "app.py").write_text("alpha\nBeta\n", encoding="utf-8")
            tools = ToolRegistry(workspace)

            result = tools.run("search_text", {"query": "beta", "path": ".", "max_results": 5})

            self.assertTrue(result["ok"])
            self.assertEqual(result["metadata"]["match_count"], 1)
            self.assertEqual(result["metadata"]["matches"][0]["path"], "app.py")
            self.assertEqual(result["metadata"]["matches"][0]["line"], 2)

    def test_file_info_reports_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "note.txt").write_text("hello", encoding="utf-8")
            tools = ToolRegistry(workspace)

            result = tools.run("file_info", {"path": "note.txt"})

            self.assertTrue(result["ok"])
            self.assertTrue(result["metadata"]["exists"])
            self.assertEqual(result["metadata"]["type"], "file")
            self.assertEqual(result["metadata"]["size_bytes"], 5)

    def test_git_status_and_diff_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            subprocess_result = subprocess.run(
                ["git", "init"],
                cwd=workspace,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            self.assertEqual(subprocess_result.returncode, 0)
            (workspace / "app.py").write_text("print('hello')\n", encoding="utf-8")
            tools = ToolRegistry(workspace)

            status = tools.run("git_status", {})
            diff = tools.run("git_diff", {})

            self.assertTrue(status["ok"])
            self.assertIn("app.py", status["output"])
            self.assertTrue(diff["ok"])

    def test_discover_and_run_recommended_verification_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "pyproject.toml").write_text("[project]\nname = \"demo\"\n", encoding="utf-8")
            tests_dir = workspace / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_smoke.py").write_text(
                "import unittest\n\n"
                "class SmokeTest(unittest.TestCase):\n"
                "    def test_smoke(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            tools = ToolRegistry(workspace)

            discovered = tools.run("discover_verification", {})
            verified = tools.run("run_recommended_verification", {"index": 1, "timeout": 30})

            self.assertTrue(discovered["ok"])
            self.assertIn("python -m unittest discover -s tests -v", discovered["output"])
            self.assertTrue(verified["ok"])
            self.assertTrue(verified["metadata"]["recommended_verification"])
            self.assertEqual(verified["metadata"]["returncode"], 0)

    def test_run_shell_returns_structured_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tools = ToolRegistry(Path(tmp))
            result = tools.run("run_shell", {"command": "python --version", "timeout": 30})

            self.assertTrue(result["ok"])
            self.assertEqual(result["metadata"]["returncode"], 0)
            self.assertIn("python --version", result["metadata"]["command"])

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
                    '{"type":"tool","tool":"write_file","args":{"path":"note.txt","content":"ok"}}',
                    '{"type":"tool","tool":"read_file","args":{"path":"note.txt"}}',
                    '{"type":"final","message":"已创建 note.txt 并复查"}',
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
                        "创建 note.txt",
                    ]
                )

            report = json.loads(report_path.read_text(encoding="utf-8"))

            self.assertEqual(exit_code, 0)
            self.assertEqual(report["final_message"], "已创建 note.txt 并复查")
            self.assertTrue(report["completion_check"]["accepted"])
            self.assertTrue(report["completion_check"]["evidence_summary"])
            self.assertTrue(report["completion_check"]["criteria_results"])
            self.assertEqual(report["completion_check"]["evidence"][0]["tool"], "write_file")
            self.assertEqual(report["completion_check"]["changed_files"][0]["path"], "note.txt")


if __name__ == "__main__":
    unittest.main()
