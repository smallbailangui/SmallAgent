"""SmallAgent 的命令行入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent import AgentConfig, CodingAgent
from .config import load_dotenv
from .model import OpenAICompatibleClient
from .tools import ToolRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smallagent",
        description="运行一个最小可用的本地编程智能体。",
    )
    parser.add_argument("task", help="交给智能体完成的编程任务。")
    parser.add_argument(
        "--workspace",
        default=".",
        help="允许智能体查看和修改的工作目录，默认为当前目录。",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=12,
        help="停止前最多执行多少轮模型和工具交互。",
    )
    parser.add_argument(
        "--history-file",
        help="可选：保存完整对话和工具观察的 JSON 文件路径。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = Path(args.workspace).resolve()

    load_dotenv()
    client = OpenAICompatibleClient.from_env()
    tools = ToolRegistry(workspace)
    agent = CodingAgent(client, tools, AgentConfig(max_steps=args.max_steps))
    result = agent.run(args.task)

    print(result.final_message)
    print(f"\n执行轮数: {result.steps}")
    if args.history_file:
        history_path = Path(args.history_file).resolve()
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(
            json.dumps(result.history, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        print(f"历史记录: {history_path}")
    return 0
