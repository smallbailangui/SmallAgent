"""交互式终端会话。

这个模块把单次任务 agent 包装成可以持续输入的 REPL：
用户每输入一条普通任务，就创建一个 CodingAgent 执行；输入 /help、/status 等
斜杠命令时，则由终端会话层直接处理，不请求模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TextIO

from .agent import AgentConfig, AgentResult, ChatClient, CodingAgent
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
    summaries: list[TaskSummary] = field(default_factory=list)

    def run_forever(self) -> int:
        """进入 REPL 循环，直到用户输入 /exit 或 /quit。"""
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

            self._run_task(task)

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

        self._print(f"未知命令：{command}。输入 /help 查看可用命令。")
        return False

    def _run_task(self, task: str) -> AgentResult:
        """把普通用户输入交给 CodingAgent 执行，并记录任务摘要。"""
        agent = CodingAgent(self.client, self.tools, self.config)
        result = agent.run(task)
        accepted = result.completion_check.accepted if result.completion_check else None
        self.summaries.append(
            TaskSummary(
                task=task,
                final_message=result.final_message,
                steps=result.steps,
                accepted=accepted,
            )
        )
        self._print(result.final_message)
        self._print(f"执行轮数: {result.steps}")
        return result

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
                    "/exit    退出交互模式",
                ]
            )
        )

    def _print_status(self) -> None:
        """显示当前终端会话状态，不请求模型。"""
        self._print(
            "\n".join(
                [
                    f"工作区：{self.tools.workspace}",
                    f"最大轮数：{self.config.max_steps}",
                    f"已完成任务数：{len(self.summaries)}",
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


def create_terminal_session(
    client: ChatClient,
    workspace: Path,
    config: AgentConfig,
    input_func: InputFunc | None = None,
    output: TextIO | None = None,
) -> TerminalSession:
    """从 CLI 参数创建交互式会话对象。"""
    return TerminalSession(
        client=client,
        tools=ToolRegistry(workspace),
        config=config,
        # 不在函数签名里绑定 input，测试才能通过 patch builtins.input 模拟终端输入。
        input_func=input if input_func is None else input_func,
        output=output,
    )
