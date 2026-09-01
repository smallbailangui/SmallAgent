"""SmallAgent 的命令行入口。

这个文件只负责“启动一次 agent 运行”：
1. 解析用户在命令行传入的任务和参数。
2. 读取 .env 配置，创建模型客户端和工具注册表。
3. 根据参数选择单次任务模式或交互式终端模式。
4. 单次任务完成后，把结果打印或保存成 JSON。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent import AgentConfig, CodingAgent
from .config import load_dotenv
from .model import OpenAICompatibleClient
from .terminal import create_terminal_session
from .tools import ToolRegistry


def build_parser() -> argparse.ArgumentParser:
    """声明命令行支持哪些参数。

    argparse 会把这些声明转换成 args.task、args.workspace 等字段，
    main() 后续只需要读取 args 即可，不用自己解析字符串。
    """
    parser = argparse.ArgumentParser(
        prog="smallagent",
        description="运行一个最小可用的本地编程智能体。",
    )
    parser.add_argument("task", nargs="?", help="交给智能体完成的编程任务。")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="进入可持续输入任务的交互式终端模式。",
    )
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
    parser.add_argument(
        "--report-file",
        help="可选：保存最终结果和完成度检查报告的 JSON 文件路径。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 主流程：组装依赖，然后运行 agent。

    argv 主要用于测试；真实命令行运行时传 None，argparse 会自动读取 sys.argv。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.interactive and not args.task:
        parser.error("the following arguments are required: task unless --interactive is used")

    # workspace 是工具允许访问的根目录，ToolRegistry 会把所有路径限制在这里。
    workspace = Path(args.workspace).resolve()

    # load_dotenv 只负责把 .env 写入环境变量；具体读哪些变量由 model.py 决定。
    load_dotenv()
    client = OpenAICompatibleClient.from_env()
    config = AgentConfig(max_steps=args.max_steps)
    if args.interactive:
        # 交互式模式复用同一个 client/workspace/config，多条用户输入连续执行。
        # 如果传入 history/report 文件，终端层会把每条任务追加成 JSON 数组记录。
        return create_terminal_session(
            client,
            workspace,
            config,
            history_file=Path(args.history_file).resolve() if args.history_file else None,
            report_file=Path(args.report_file).resolve() if args.report_file else None,
        ).run_forever()

    tools = ToolRegistry(workspace)
    agent = CodingAgent(client, tools, config)
    assert args.task is not None
    result = agent.run(args.task)

    print(result.final_message)
    print(f"\n执行轮数: {result.steps}")
    if args.history_file:
        # history 保存完整消息轨迹，适合复盘模型每一步 JSON 和工具观察。
        history_path = Path(args.history_file).resolve()
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(
            json.dumps(result.history, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        print(f"历史记录: {history_path}")
    if args.report_file:
        # report 保存最终回答和 harness 的结构化判断，适合后续自动评分。
        report_path = Path(args.report_file).resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "final_message": result.final_message,
            "steps": result.steps,
            "completion_check": result.completion_check.to_dict()
            if result.completion_check
            else None,
        }
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        print(f"完成度报告: {report_path}")
    return 0
