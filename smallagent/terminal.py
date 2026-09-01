"""交互式终端会话。

这个模块把单次任务 agent 包装成可以持续输入的 REPL：
用户每输入一条普通任务，就创建一个 CodingAgent 执行；输入 /help、/status 等
斜杠命令时，则由终端会话层直接处理，不请求模型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TextIO

from .agent import AgentConfig, AgentResult, ChatClient, CodingAgent
from .decision import Decision
from .tools import ToolRegistry


InputFunc = Callable[[str], str]


@dataclass
class TaskSummary:
    """交互式会话里一条已完成任务的摘要。"""

    task: str
    final_message: str
    steps: int
    accepted: bool | None

    def to_line(self, index: int) -> str:
        """渲染成 /history 命令显示的一行文本。"""
        status = "通过" if self.accepted else "未通过" if self.accepted is False else "未知"
        return f"{index}. [{status}] {self.task}（{self.steps} 轮）"

    def to_dict(self) -> dict[str, object]:
        """转换为可写入 JSON 的稳定结构。"""
        return {
            "task": self.task,
            "final_message": self.final_message,
            "steps": self.steps,
            "accepted": self.accepted,
        }


@dataclass
class TerminalSession:
    """SmallAgent 的交互式终端外壳。

    它只管理输入输出和会话摘要；真正的模型推理、工具执行、完成度检查仍由
    CodingAgent.run() 完成。input_func/output 允许测试注入假输入和捕获输出。
    """

    client: ChatClient
    tools: ToolRegistry
    config: AgentConfig
    input_func: InputFunc = input
    output: TextIO | None = None
    history_file: Path | None = None
    report_file: Path | None = None
    summaries: list[TaskSummary] = field(default_factory=list)

    def run_forever(self) -> int:
        """进入 REPL 循环，直到用户输入 /exit 或 /quit。"""
        self.load_existing_summaries()
        self._print("SmallAgent 交互模式，输入 /help 查看命令，输入 /exit 退出。")
        while True:
            try:
                raw = self.input_func("SmallAgent> ")
            except EOFError:
                self._print("")
                return 0

            task = raw.strip()
            if not task:
                continue
            if task.startswith("/"):
                if self._handle_command(task):
                    return 0
                continue

            try:
                self._run_task(task)
            except Exception as exc:  # noqa: BLE001 - 交互终端要隔离单个任务失败，不能直接退出
                self._record_task_failure(task, exc)

    def load_existing_summaries(self) -> None:
        """从已有 report/history 文件恢复历史摘要。

        优先读取 report_file，因为它本来就包含任务、final、轮数和验收状态；
        如果没有 report_file，再尝试从 history_file 里的摘要字段恢复。
        """
        if self.summaries:
            return
        source = self.report_file if self.report_file and self.report_file.exists() else self.history_file
        if source is None or not source.exists():
            return
        try:
            records = self._read_json_records(source)
        except ValueError as exc:
            self._print(f"历史摘要加载失败：{exc}")
            return

        for record in records:
            summary = self._summary_from_record(record)
            if summary is not None:
                self.summaries.append(summary)
        if self.summaries:
            self._print(f"已恢复历史任务摘要：{len(self.summaries)} 条。")

    def _handle_command(self, command: str) -> bool:
        """处理斜杠命令。

        返回 True 表示会话应该退出；返回 False 表示继续等待下一条输入。
        """
        normalized = command.lower()
        if normalized in {"/exit", "/quit"}:
            self._print("已退出 SmallAgent。")
            return True
        if normalized == "/help":
            self._print_help()
            return False
        if normalized == "/status":
            self._print_status()
            return False
        if normalized == "/history":
            self._print_history()
            return False
        if normalized == "/clear":
            self.summaries.clear()
            self._print("已清空当前终端会话摘要。")
            return False
        if normalized == "/retry":
            self._retry_last_task()
            return False

        self._print(f"未知命令：{command}。输入 /help 查看可用命令。")
        return False

    def _run_task(self, task: str) -> AgentResult:
        """把普通用户输入交给 CodingAgent 执行，并记录任务摘要。"""
        agent = CodingAgent(self.client, self.tools, self.config, approval_callback=self._approve_tool)
        result = agent.run(task, self._session_context_prompt())
        accepted = result.completion_check.accepted if result.completion_check else None
        summary = TaskSummary(
            task=task,
            final_message=result.final_message,
            steps=result.steps,
            accepted=accepted,
        )
        self.summaries.append(summary)
        self._save_task_artifacts(summary, result)
        self._print(result.final_message)
        self._print(f"执行轮数: {result.steps}")
        return result

    def _retry_last_task(self) -> None:
        """重新执行最近一条任务。

        /retry 复用 summary 里保存的原始任务文本，适合模型空响应、网络中断或用户拒绝命令后
        想快速再试一次的场景。重试本身会生成新的 summary 和记录文件条目。
        """
        if not self.summaries:
            self._print("暂无可重试任务。")
            return
        task = self.summaries[-1].task
        self._print(f"重试任务：{task}")
        try:
            self._run_task(task)
        except Exception as exc:  # noqa: BLE001 - /retry 也要保持交互终端不退出
            self._record_task_failure(task, exc)

    def _session_context_prompt(self) -> str:
        """生成给下一条任务使用的会话摘要。

        这里只放最近 5 条任务的简短结果，避免终端会话越聊越长时撑爆模型上下文。
        """
        if not self.summaries:
            return ""
        lines = ["SESSION_CONTEXT:", "本终端会话最近任务摘要："]
        start = max(0, len(self.summaries) - 5)
        for index, summary in enumerate(self.summaries[start:], start + 1):
            lines.append(summary.to_line(index))
            lines.append(f"- 结果：{summary.final_message[:240]}")
        return "\n".join(lines)

    def _save_task_artifacts(self, summary: TaskSummary, result: AgentResult) -> None:
        """按需保存交互式任务的 history 和 report。

        交互模式会连续运行多个任务，所以文件内容使用 JSON 数组；每完成一条任务就追加一条记录。
        """
        task_index = len(self.summaries)
        if self.history_file is not None:
            self._append_json_record(
                self.history_file,
                {
                    "task_index": task_index,
                    **summary.to_dict(),
                    "history": result.history,
                },
            )
        if self.report_file is not None:
            self._append_json_record(
                self.report_file,
                {
                    "task_index": task_index,
                    **summary.to_dict(),
                    "completion_check": result.completion_check.to_dict()
                    if result.completion_check
                    else None,
                },
            )

    def _record_task_failure(self, task: str, exc: Exception) -> None:
        """记录一次没有形成 AgentResult 的任务失败。

        网络中断、模型服务异常、记录文件损坏等问题都可能发生在 agent 返回结果前。
        交互式终端把这类失败压成摘要和 report 记录，然后继续等待用户下一条输入。
        """
        message = f"{type(exc).__name__}: {exc}"
        summary = TaskSummary(task=task, final_message=message, steps=0, accepted=False)
        self.summaries.append(summary)
        task_index = len(self.summaries)
        if self.history_file is not None:
            self._append_json_record(
                self.history_file,
                {
                    "task_index": task_index,
                    **summary.to_dict(),
                    "history": [],
                    "error": message,
                },
            )
        if self.report_file is not None:
            self._append_json_record(
                self.report_file,
                {
                    "task_index": task_index,
                    **summary.to_dict(),
                    "completion_check": None,
                    "error": message,
                },
            )
        self._print(f"任务失败：{message}")
        self._print("交互模式仍在运行，可以重试或输入 /exit 退出。")

    def _approve_tool(self, action: dict[str, object], decision: Decision) -> bool:
        """在交互模式下确认高风险工具调用。

        当前 high 风险主要是 run_shell 和 run_recommended_verification。用户输入 y/yes
        才允许执行；直接回车或其他输入都会拒绝。
        """
        tool = str(action.get("tool", ""))
        args = action.get("args", {})
        command = ""
        if isinstance(args, dict) and isinstance(args.get("command"), str):
            command = str(args["command"])
        self._print(f"即将执行高风险工具：{tool}")
        self._print(f"风险等级：{decision.risk}")
        if command:
            self._print(f"命令：{command}")
        answer = self.input_func("是否允许执行？y/N: ").strip().lower()
        allowed = answer in {"y", "yes"}
        if not allowed:
            self._print("已拒绝执行该工具。")
        return allowed

    def _print_help(self) -> None:
        """显示终端内置命令。"""
        self._print(
            "\n".join(
                [
                    "可用命令：",
                    "/help    显示帮助",
                    "/status  显示当前工作区和会话状态",
                    "/history 显示本次终端会话的任务摘要",
                    "/clear   清空本次终端会话摘要",
                    "/retry   重新执行最近一条任务",
                    "/exit    退出交互模式",
                ]
            )
        )

    def _print_status(self) -> None:
        """显示当前终端会话状态，不请求模型。"""
        model = getattr(self.client, "model", "<unknown>")
        base_url = getattr(self.client, "base_url", "<unknown>")
        self._print(
            "\n".join(
                [
                    f"工作区：{self.tools.workspace}",
                    f"模型：{model}",
                    f"接口地址：{base_url}",
                    f"最大轮数：{self.config.max_steps}",
                    f"已完成任务数：{len(self.summaries)}",
                    f"History 文件：{self.history_file or '未启用'}",
                    f"Report 文件：{self.report_file or '未启用'}",
                ]
            )
        )

    def _print_history(self) -> None:
        """显示本次终端会话内完成过的任务。"""
        if not self.summaries:
            self._print("暂无任务历史。")
            return
        lines = ["任务历史："]
        for index, summary in enumerate(self.summaries, 1):
            lines.append(summary.to_line(index))
            lines.append(f"   结果：{summary.final_message[:160]}")
        self._print("\n".join(lines))

    def _print(self, message: str = "") -> None:
        """统一输出函数，便于测试捕获终端输出。"""
        if self.output is None:
            print(message)
        else:
            print(message, file=self.output)

    def _append_json_record(self, path: Path, record: dict[str, object]) -> None:
        """向 JSON 数组文件追加一条记录。

        文件不存在时创建新数组；文件存在但为空时也按空数组处理。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self._read_json_records(path) if path.exists() else []
        data.append(record)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    def _read_json_records(self, path: Path) -> list[dict[str, object]]:
        """读取交互式记录文件，确保内容是 JSON 对象数组。"""
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return []
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError(f"{path} must contain a JSON array")
        records: list[dict[str, object]] = []
        for item in data:
            if isinstance(item, dict):
                records.append(item)
        return records

    def _summary_from_record(self, record: dict[str, object]) -> TaskSummary | None:
        """从持久化记录中恢复 /history 需要的轻量摘要。"""
        task = record.get("task")
        final_message = record.get("final_message")
        steps = record.get("steps", 0)
        accepted = record.get("accepted")
        if not isinstance(task, str) or not isinstance(final_message, str):
            return None
        if not isinstance(steps, int):
            steps = 0
        if not isinstance(accepted, bool):
            accepted = None
        return TaskSummary(task=task, final_message=final_message, steps=steps, accepted=accepted)


def create_terminal_session(
    client: ChatClient,
    workspace: Path,
    config: AgentConfig,
    input_func: InputFunc | None = None,
    output: TextIO | None = None,
    history_file: Path | None = None,
    report_file: Path | None = None,
) -> TerminalSession:
    """从 CLI 参数创建交互式会话对象。"""
    return TerminalSession(
        client=client,
        tools=ToolRegistry(workspace),
        config=config,
        # 不在函数签名里绑定 input，测试才能通过 patch builtins.input 模拟终端输入。
        input_func=input if input_func is None else input_func,
        output=output,
        history_file=history_file,
        report_file=report_file,
    )
