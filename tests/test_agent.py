from __future__ import annotations

import io
import json
import http.client
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
from smallagent.model import OpenAICompatibleClient
from smallagent.perception import Perception
from smallagent.planning import Planner
from smallagent.terminal import TerminalSession
from smallagent.tools import ToolRegistry


class FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0
        self.messages_seen: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.messages_seen.append([dict(message) for message in messages])
        response = self.responses[self.calls]
        self.calls += 1
        return response


class FailingClient:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.model = "failing-model"
        self.base_url = "https://example.invalid/v1"

    def complete(self, messages: list[dict[str, str]]) -> str:
        raise self.error


class IncompleteReadResponse:
    def __enter__(self) -> "IncompleteReadResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        raise http.client.IncompleteRead(b"", 10)


class JsonResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "JsonResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


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

    def test_agent_accepts_session_context_before_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(
                [
                    '{"type":"tool","tool":"get_cwd","args":{}}',
                    '{"type":"final","message":"完成"}',
                ]
            )
            agent = CodingAgent(client, ToolRegistry(Path(tmp)), AgentConfig(max_steps=3))

            agent.run("继续上一个任务", session_context="SESSION_CONTEXT:\n上一轮修改了 README")

            first_messages = client.messages_seen[0]

            self.assertIn("SESSION_CONTEXT", first_messages[-2]["content"])
            self.assertEqual(first_messages[-1]["content"], "继续上一个任务")

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

    def test_agent_asks_approval_before_high_risk_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            approvals: list[str] = []
            client = FakeClient(
                [
                    '{"type":"tool","tool":"run_shell","args":{"command":"python --version"}}',
                    '{"type":"final","message":"已执行检查"}',
                ]
            )

            def approve(action: dict[str, object], decision: object) -> bool:
                approvals.append(str(action.get("tool")))
                return True

            agent = CodingAgent(
                client,
                ToolRegistry(Path(tmp)),
                AgentConfig(max_steps=3),
                approval_callback=approve,
            )

            result = agent.run("查看目录")

            self.assertEqual(approvals, ["run_shell"])
            self.assertEqual(result.final_message, "已执行检查")

    def test_agent_reports_denied_high_risk_tool_to_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(
                [
                    '{"type":"tool","tool":"run_shell","args":{"command":"python --version"}}',
                    '{"type":"tool","tool":"get_cwd","args":{}}',
                    '{"type":"final","message":"已改用安全方式查看目录"}',
                ]
            )
            agent = CodingAgent(
                client,
                ToolRegistry(Path(tmp)),
                AgentConfig(max_steps=4),
                approval_callback=lambda action, decision: False,
            )

            result = agent.run("查看目录")
            joined_history = "\n".join(item["content"] for item in result.history)

            self.assertIn("用户拒绝执行高风险工具 run_shell", joined_history)
            self.assertEqual(result.final_message, "已改用安全方式查看目录")


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
        self.assertEqual(policy.decide({"type": "tool", "tool": "patch_file", "args": {}}).risk, "medium")
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

    def test_completion_harness_marks_patch_file_as_mutation(self) -> None:
        harness = CompletionHarness()
        harness.start_task("修改 app.py")

        harness.record_tool_call(
            "patch_file",
            {"path": "app.py", "patch": "@@ -1,1 +1,1 @@\n-old\n+new\n"},
            {
                "tool": "patch_file",
                "ok": True,
                "output": "applied 1 patch hunk(s) to app.py",
                "error": "",
                "metadata": {"path": "app.py", "hunk_count": 1},
            },
        )

        self.assertIn("mutation", harness.evidence[0].tags)
        self.assertEqual(harness.evidence[0].details["path"], "app.py")

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

    def test_completion_harness_detects_directory_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            harness = CompletionHarness()
            harness.start_task("创建 docs 目录", workspace)

            (workspace / "docs").mkdir()
            harness.record_tool_call(
                "create_directory",
                {"path": "docs"},
                {"tool": "create_directory", "ok": True, "output": "created directory docs", "error": ""},
            )
            harness.record_tool_call(
                "list_files",
                {"path": "."},
                {"tool": "list_files", "ok": True, "output": "docs/", "error": ""},
            )
            changes = {item.path: item.status for item in harness.changed_files()}
            check = harness.evaluate("已创建 docs 目录")

            self.assertEqual(changes["docs/"], "added")
            self.assertTrue(check.accepted)

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

    def test_create_directory_and_append_text_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            tools = ToolRegistry(workspace)

            created = tools.run("create_directory", {"path": "notes"})
            appended = tools.run(
                "append_text",
                {"path": "notes/log.txt", "content": "first\n", "create": True},
            )
            appended_again = tools.run(
                "append_text",
                {"path": "notes/log.txt", "content": "second\n", "create": False},
            )

            self.assertTrue(created["ok"])
            self.assertEqual(created["metadata"]["path"], "notes")
            self.assertTrue(appended["ok"])
            self.assertTrue(appended_again["ok"])
            self.assertEqual((workspace / "notes" / "log.txt").read_text(encoding="utf-8"), "first\nsecond\n")

    def test_insert_text_and_replace_lines_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "app.py"
            target.write_text("one\nthree\nfour\n", encoding="utf-8")
            tools = ToolRegistry(workspace)

            inserted = tools.run("insert_text", {"path": "app.py", "line": 2, "content": "two\n"})
            replaced = tools.run(
                "replace_lines",
                {"path": "app.py", "start": 3, "end": 4, "content": "THREE\nFOUR\n"},
            )

            self.assertTrue(inserted["ok"])
            self.assertEqual(inserted["metadata"]["line"], 2)
            self.assertTrue(replaced["ok"])
            self.assertEqual(replaced["metadata"]["start"], 3)
            self.assertEqual(target.read_text(encoding="utf-8"), "one\ntwo\nTHREE\nFOUR\n")

    def test_line_edit_tools_validate_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "app.py").write_text("one\n", encoding="utf-8")
            tools = ToolRegistry(workspace)

            inserted = tools.run("insert_text", {"path": "app.py", "line": 3, "content": "bad\n"})
            replaced = tools.run("replace_lines", {"path": "app.py", "start": 2, "end": 2, "content": "bad\n"})

            self.assertFalse(inserted["ok"])
            self.assertIn("line must be", inserted["error"])
            self.assertFalse(replaced["ok"])
            self.assertIn("start/end", replaced["error"])

    def test_patch_file_applies_single_file_unified_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "app.py"
            target.write_text("one\ntwo\nthree\n", encoding="utf-8")
            tools = ToolRegistry(workspace)
            patch_text = (
                "--- a/app.py\n"
                "+++ b/app.py\n"
                "@@ -1,3 +1,4 @@\n"
                " one\n"
                "-two\n"
                "+TWO\n"
                "+inserted\n"
                " three\n"
            )

            result = tools.run("patch_file", {"path": "app.py", "patch": patch_text})

            self.assertTrue(result["ok"])
            self.assertEqual(target.read_text(encoding="utf-8"), "one\nTWO\ninserted\nthree\n")
            self.assertEqual(result["metadata"]["path"], "app.py")
            self.assertEqual(result["metadata"]["hunk_count"], 1)
            self.assertEqual(result["metadata"]["added_lines"], 2)
            self.assertEqual(result["metadata"]["removed_lines"], 1)

    def test_patch_file_rejects_mismatched_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "app.py"
            target.write_text("one\ntwo\n", encoding="utf-8")
            tools = ToolRegistry(workspace)
            patch_text = (
                "@@ -1,2 +1,2 @@\n"
                " one\n"
                "-missing\n"
                "+TWO\n"
            )

            result = tools.run("patch_file", {"path": "app.py", "patch": patch_text})

            self.assertFalse(result["ok"])
            self.assertIn("patch context mismatch", result["error"])
            self.assertEqual(target.read_text(encoding="utf-8"), "one\ntwo\n")

    def test_patch_file_rejects_header_for_another_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "app.py").write_text("one\n", encoding="utf-8")
            tools = ToolRegistry(workspace)
            patch_text = (
                "--- a/other.py\n"
                "+++ b/other.py\n"
                "@@ -1,1 +1,1 @@\n"
                "-one\n"
                "+two\n"
            )

            result = tools.run("patch_file", {"path": "app.py", "patch": patch_text})

            self.assertFalse(result["ok"])
            self.assertIn("does not match target path", result["error"])

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


class ModelClientTests(unittest.TestCase):
    def test_model_client_accepts_base_url_alias(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "test-key",
                "SMALLAGENT_MODEL": "demo-model",
                "BASE_URL": "https://api.example.com/",
            },
            clear=True,
        ):
            client = OpenAICompatibleClient.from_env()

            self.assertEqual(client.base_url, "https://api.example.com")

    def test_model_client_wraps_incomplete_read(self) -> None:
        client = OpenAICompatibleClient(api_key="test-key", model="test-model", max_retries=0)

        with patch("urllib.request.urlopen", return_value=IncompleteReadResponse()):
            with self.assertRaisesRegex(RuntimeError, "response ended before"):
                client.complete([{"role": "user", "content": "hello"}])

    def test_model_client_retries_empty_content_once(self) -> None:
        client = OpenAICompatibleClient(api_key="test-key", model="test-model", max_retries=1)
        empty = JsonResponse({"choices": [{"message": {"content": ""}}]})
        valid = JsonResponse({"choices": [{"message": {"content": "ok"}}]})

        with patch("urllib.request.urlopen", side_effect=[empty, valid]) as urlopen:
            content = client.complete([{"role": "user", "content": "hello"}])

            self.assertEqual(content, "ok")
            self.assertEqual(urlopen.call_count, 2)


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

    def test_cli_interactive_mode_runs_terminal_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history_path = Path(tmp) / "interactive-history.json"
            report_path = Path(tmp) / "interactive-report.json"
            client = FakeClient(
                [
                    '{"type":"tool","tool":"get_cwd","args":{}}',
                    '{"type":"final","message":"已查看目录"}',
                ]
            )
            inputs = iter(["查看目录", "/history", "/exit"])
            output = io.StringIO()

            with (
                patch("smallagent.cli.OpenAICompatibleClient.from_env", return_value=client),
                patch("builtins.input", side_effect=lambda prompt: next(inputs)),
                redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "--workspace",
                        tmp,
                        "--history-file",
                        str(history_path),
                        "--report-file",
                        str(report_path),
                        "--interactive",
                    ]
                )

            rendered = output.getvalue()
            history = json.loads(history_path.read_text(encoding="utf-8"))
            report = json.loads(report_path.read_text(encoding="utf-8"))

            self.assertEqual(exit_code, 0)
            self.assertIn("SmallAgent 交互模式", rendered)
            self.assertIn("已查看目录", rendered)
            self.assertIn("任务历史", rendered)
            self.assertEqual(history[0]["task"], "查看目录")
            self.assertTrue(history[0]["history"])
            self.assertEqual(report[0]["task_index"], 1)
            self.assertTrue(report[0]["completion_check"]["accepted"])


class TerminalSessionTests(unittest.TestCase):
    def test_terminal_session_handles_builtin_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            inputs = iter(["/status", "/history", "/retry", "/clear", "/unknown", "/exit"])
            session = TerminalSession(
                client=FakeClient([]),
                tools=ToolRegistry(Path(tmp)),
                config=AgentConfig(max_steps=2),
                input_func=lambda prompt: next(inputs),
                output=output,
            )

            exit_code = session.run_forever()
            rendered = output.getvalue()

            self.assertEqual(exit_code, 0)
            self.assertIn("工作区", rendered)
            self.assertIn("暂无任务历史", rendered)
            self.assertIn("暂无可重试任务", rendered)
            self.assertIn("已清空当前终端会话摘要", rendered)
            self.assertIn("未知命令", rendered)

    def test_terminal_session_keeps_running_after_task_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.json"
            output = io.StringIO()
            inputs = iter(["会触发模型错误", "/history", "/exit"])
            session = TerminalSession(
                client=FailingClient(RuntimeError("temporary model failure")),
                tools=ToolRegistry(Path(tmp)),
                config=AgentConfig(max_steps=2),
                input_func=lambda prompt: next(inputs),
                output=output,
                report_file=report_path,
            )

            exit_code = session.run_forever()
            rendered = output.getvalue()
            report = json.loads(report_path.read_text(encoding="utf-8"))

            self.assertEqual(exit_code, 0)
            self.assertIn("任务失败", rendered)
            self.assertIn("交互模式仍在运行", rendered)
            self.assertIn("任务历史", rendered)
            self.assertFalse(session.summaries[0].accepted)
            self.assertEqual(report[0]["error"], "RuntimeError: temporary model failure")

    def test_terminal_session_prompts_for_high_risk_tool_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            client = FakeClient(
                [
                    '{"type":"tool","tool":"run_shell","args":{"command":"python --version"}}',
                    '{"type":"final","message":"检查完成"}',
                ]
            )
            inputs = iter(["运行版本检查", "y", "/exit"])
            session = TerminalSession(
                client=client,
                tools=ToolRegistry(Path(tmp)),
                config=AgentConfig(max_steps=3),
                input_func=lambda prompt: next(inputs),
                output=output,
            )

            session.run_forever()
            rendered = output.getvalue()

            self.assertIn("即将执行高风险工具：run_shell", rendered)
            self.assertIn("检查完成", rendered)

    def test_terminal_session_can_deny_high_risk_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            client = FakeClient(
                [
                    '{"type":"tool","tool":"run_shell","args":{"command":"python --version"}}',
                    '{"type":"tool","tool":"get_cwd","args":{}}',
                    '{"type":"final","message":"已跳过命令"}',
                ]
            )
            inputs = iter(["查看目录", "n", "/exit"])
            session = TerminalSession(
                client=client,
                tools=ToolRegistry(Path(tmp)),
                config=AgentConfig(max_steps=4),
                input_func=lambda prompt: next(inputs),
                output=output,
            )

            session.run_forever()
            rendered = output.getvalue()

            self.assertIn("已拒绝执行该工具", rendered)
            self.assertIn("已跳过命令", rendered)

    def test_terminal_session_retries_last_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            client = FakeClient(
                [
                    '{"type":"tool","tool":"get_cwd","args":{}}',
                    '{"type":"final","message":"第一次完成"}',
                    '{"type":"tool","tool":"get_cwd","args":{}}',
                    '{"type":"final","message":"重试完成"}',
                ]
            )
            inputs = iter(["查看目录", "/retry", "/history", "/exit"])
            session = TerminalSession(
                client=client,
                tools=ToolRegistry(Path(tmp)),
                config=AgentConfig(max_steps=3),
                input_func=lambda prompt: next(inputs),
                output=output,
            )

            session.run_forever()
            rendered = output.getvalue()

            self.assertEqual(len(session.summaries), 2)
            self.assertEqual(session.summaries[0].task, "查看目录")
            self.assertEqual(session.summaries[1].task, "查看目录")
            self.assertIn("重试任务：查看目录", rendered)
            self.assertIn("重试完成", rendered)

    def test_terminal_session_runs_multiple_tasks_and_keeps_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history_path = Path(tmp) / "history.json"
            report_path = Path(tmp) / "report.json"
            output = io.StringIO()
            client = FakeClient(
                [
                    '{"type":"tool","tool":"get_cwd","args":{}}',
                    '{"type":"final","message":"第一个任务完成"}',
                    '{"type":"tool","tool":"get_cwd","args":{}}',
                    '{"type":"final","message":"第二个任务完成"}',
                ]
            )
            inputs = iter(["查看目录", "再查看一次目录", "/history", "/exit"])
            session = TerminalSession(
                client=client,
                tools=ToolRegistry(Path(tmp)),
                config=AgentConfig(max_steps=3),
                input_func=lambda prompt: next(inputs),
                output=output,
                history_file=history_path,
                report_file=report_path,
            )

            session.run_forever()
            rendered = output.getvalue()
            history = json.loads(history_path.read_text(encoding="utf-8"))
            report = json.loads(report_path.read_text(encoding="utf-8"))

            self.assertEqual(len(session.summaries), 2)
            self.assertIn("第一个任务完成", rendered)
            self.assertIn("第二个任务完成", rendered)
            self.assertIn("1. [通过] 查看目录", rendered)
            self.assertIn("2. [通过] 再查看一次目录", rendered)
            self.assertEqual(len(history), 2)
            self.assertEqual(len(report), 2)
            self.assertEqual(report[1]["task"], "再查看一次目录")
            self.assertIn("SESSION_CONTEXT", client.messages_seen[2][-2]["content"])

    def test_terminal_session_loads_existing_report_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.json"
            report_path.write_text(
                json.dumps(
                    [
                        {
                            "task_index": 1,
                            "task": "之前的任务",
                            "final_message": "之前已经完成",
                            "steps": 2,
                            "accepted": True,
                            "completion_check": {"accepted": True},
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            inputs = iter(["/history", "/exit"])
            session = TerminalSession(
                client=FakeClient([]),
                tools=ToolRegistry(Path(tmp)),
                config=AgentConfig(max_steps=3),
                input_func=lambda prompt: next(inputs),
                output=output,
                report_file=report_path,
            )

            session.run_forever()
            rendered = output.getvalue()

            self.assertEqual(len(session.summaries), 1)
            self.assertIn("已恢复历史任务摘要：1 条", rendered)
            self.assertIn("之前的任务", rendered)


if __name__ == "__main__":
    unittest.main()
