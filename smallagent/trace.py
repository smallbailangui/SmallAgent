"""运行轨迹的终端格式化。"""

from __future__ import annotations

import json
from typing import Any


def format_trace_event(event: dict[str, Any]) -> str:
    """把 agent 内部 trace 事件压缩成适合录屏展示的分块文本。"""
    kind = event.get("event")
    step = event.get("step")
    round_title = f"第 {step} 轮" if step is not None else "执行过程"

    if kind == "step_started":
        return _block(
            f"{round_title} | Agent 状态汇总（Perception / Planning / Memory）",
            [
                f"感知：{event.get('perception', '')}",
                f"规划：{event.get('plan', '')}",
                f"记忆：{event.get('memory', '')}",
                f"验收：{event.get('acceptance', '')}",
                "流转：把状态交给模型，请它选择下一步。",
            ],
        )
    if kind == "parse_error":
        return _block(
            f"{round_title} | 协议修正（JSON Action Protocol）",
            [
                "协议：模型输出没有满足 JSON 动作格式。",
                "流转：把解析错误反馈给模型，请它重新给出可执行动作。",
                f"解析结果：{event.get('error', '')}",
            ],
        )
    if kind == "decision_blocked":
        return _block(
            f"{round_title} | 本地决策拦截（Decision Policy）",
            [
                "决策：模型动作没有通过本地策略校验，暂不执行。",
                f"拦截原因：{event.get('reason', '')}",
            ],
        )
    if kind == "model_action":
        if event.get("action_type") == "final":
            return _block(
                f"{round_title} | 模型行动提案与本地决策（Action Proposal / Decision）",
                [
                    "行动提案：模型认为已有足够上下文和证据，准备提交 final。",
                    f"决策：{event.get('decision', '最终回答进入完成度检查')}",
                    "模型动作：final",
                ],
            )
        tool = event.get("tool", "")
        reason = str(event.get("reason", "")).strip()
        lines = [
            f"行动提案：{reason or _tool_intent(tool)}",
            f"决策：{event.get('decision', '动作通过本地决策层校验')}",
            f"流转：进入工具执行 {tool}",
        ]
        return _block(f"{round_title} | 模型行动提案与本地决策（Action Proposal / Decision）", lines)
    if kind == "approval":
        status = "允许" if event.get("allowed") else "拒绝"
        return _block(
            f"{round_title} | 人工安全确认（Human Approval）",
            [
                "安全策略：高风险工具需要用户确认后才能继续。",
                f"工具名称：{event.get('tool', '')}",
                f"人工确认：{status}",
            ],
        )
    if kind == "tool_result":
        status = "成功" if event.get("ok") else "失败"
        detail = str(event.get("summary", "") or event.get("error", "")).replace("\n", " ")
        return _block(
            f"{round_title} | 工具执行观察（Tool Observation）",
            [
                f"Observation：工具 {event.get('tool', '')} 执行{status}",
                f"感知更新：{event.get('perception', '')}",
                f"记忆更新：{event.get('memory', '')}",
                f"规划更新：{event.get('plan', '')}",
                f"结果摘要：{_truncate(detail, 220)}",
            ],
        )
    if kind == "auto_verification":
        return _block(
            f"{round_title} | 自动补充验证（Auto Verification）",
            [
                "验收策略：最终回答还缺推荐验证证据，agent 自动补跑项目检查。",
                f"验证命令：{event.get('command', '')}",
            ],
        )
    if kind == "final_check":
        status = "通过" if event.get("accepted") else "未通过"
        reasons = event.get("reasons", [])
        lines = [
            "决策：本地完成度检查判断 final 是否有足够证据支撑。",
            f"验收结果：{status}",
        ]
        if reasons:
            lines.append(f"原因摘要：{_truncate('; '.join(map(str, reasons)), 260)}")
        return _block(f"{round_title} | Final 验收决策（Completion Check）", lines)

    return _block("执行过程", [json.dumps(event, ensure_ascii=False, sort_keys=True)])


def format_final_block(message: str, steps: int) -> str:
    """把最终回答和轮数渲染成独立总结块。"""
    return _block(
        "最终总结",
        [
            message or "Done.",
            f"执行轮数：{steps}",
        ],
    )


def _tool_intent(tool: object) -> str:
    intents = {
        "get_cwd": "确认当前受控工作区。",
        "list_files": "查看目录结构，建立任务上下文。",
        "read_file": "读取关键文件，先理解现有实现。",
        "file_info": "检查文件或目录的元信息。",
        "search_text": "搜索相关符号或文本位置。",
        "create_directory": "创建任务需要的目录。",
        "write_file": "写入新文件或重建小文件内容。",
        "append_text": "向已有文件追加内容。",
        "insert_text": "在指定位置插入文本。",
        "replace_text": "做一次精确文本替换。",
        "replace_lines": "替换指定行范围。",
        "patch_file": "应用单文件补丁完成局部修改。",
        "run_shell": "运行命令验证结果或收集环境信息。",
        "git_status": "查看 Git 工作区变化。",
        "git_diff": "查看具体代码差异。",
        "discover_verification": "发现项目推荐的验证命令。",
        "run_recommended_verification": "运行项目推荐验证命令。",
    }
    return intents.get(str(tool), "根据当前状态选择下一步可执行动作。")


def _block(title: str, lines: list[str]) -> str:
    body = "\n".join(lines)
    return f"\n========== {title} ==========\n{body}\n================================\n"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 15] + "...[truncated]"
