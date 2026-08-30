"""Command line interface for SmallAgent."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent import AgentConfig, CodingAgent
from .model import OpenAICompatibleClient
from .tools import ToolRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smallagent",
        description="Run a minimal local coding agent.",
    )
    parser.add_argument("task", help="The coding task for the agent to perform.")
    parser.add_argument(
        "--workspace",
        default=".",
        help="Workspace directory the agent may inspect and edit. Defaults to current directory.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=12,
        help="Maximum model-tool iterations before stopping.",
    )
    parser.add_argument(
        "--history-file",
        help="Optional JSON file for saving the full conversation and tool observations.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = Path(args.workspace).resolve()

    client = OpenAICompatibleClient.from_env()
    tools = ToolRegistry(workspace)
    agent = CodingAgent(client, tools, AgentConfig(max_steps=args.max_steps))
    result = agent.run(args.task)

    print(result.final_message)
    print(f"\nSteps: {result.steps}")
    if args.history_file:
        history_path = Path(args.history_file).resolve()
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(
            json.dumps(result.history, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        print(f"History: {history_path}")
    return 0
