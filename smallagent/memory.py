"""记忆层：保存任务过程中的短期工作记忆。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MemoryItem:
    """一条可复述的运行记忆。

    kind 表示记忆来源类别，content 是已经压缩过、适合放回 prompt 的事实。
    """

    kind: str
    content: str


@dataclass
class ShortTermMemory:
    """当前任务内的短期工作记忆，后续可替换为文件或向量存储。

    这里不保存完整工具输出；完整轨迹由 AgentResult.history 负责，可验收证据由
    CompletionHarness 负责。Memory 只保留对下一步决策有帮助的事实摘要。
    """

    items: list[MemoryItem] = field(default_factory=list)
    limit: int = 20

    def clear(self) -> None:
        """开始新任务时清空任务内记忆，避免复用 agent 实例时串任务。"""
        self.items.clear()

    def remember(self, kind: str, content: str) -> None:
        """追加一条记忆，并只保留最近 limit 条，避免提示词无限增长。"""
        self.items.append(MemoryItem(kind=kind, content=content))
        if len(self.items) > self.limit:
            self.items = self.items[-self.limit :]

    def remember_tool_result(self, result: dict[str, Any], args: dict[str, Any] | None = None) -> None:
        """把工具观察转成面向决策的短文本记忆。

        args 保存模型当轮传给工具的参数；result 保存工具实际返回。两者合起来比 raw
        output 更适合生成“读过哪个文件、改过哪个文件、运行了什么命令”这类记忆。
        """
        args = args or {}
        tool = str(result.get("tool", ""))
        ok = bool(result.get("ok", False))
        status = "成功" if ok else "失败"
        content = self._summarize_tool_result(tool, status, result, args)
        self.remember("tool", content)

    def to_prompt(self) -> str:
        """渲染最近记忆，供模型下一轮继续推理。"""
        if not self.items:
            return "短期记忆：暂无。"
        lines = ["短期记忆："]
        for item in self.items[-8:]:
            lines.append(f"- [{item.kind}] {item.content}")
        return "\n".join(lines)

    def _summarize_tool_result(
        self,
        tool: str,
        status: str,
        result: dict[str, Any],
        args: dict[str, Any],
    ) -> str:
        """根据工具类型提炼稳定事实，而不是机械复制工具输出。"""
        metadata = result.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}

        path = self._string_arg(args, "path") or self._string_arg(metadata, "path")
        command = self._string_arg(args, "command") or self._string_arg(metadata, "command")
        output = self._preview(str(result.get("output", "") or result.get("error", "")))

        if tool == "read_file" and path:
            return f"已读取 {path}，结果{status}；摘要：{output}"
        if tool == "list_files":
            listed_path = path or self._string_arg(args, "path") or "."
            count = len([line for line in str(result.get("output", "")).splitlines() if line.strip()])
            return f"已列出 {listed_path}，结果{status}；发现约 {count} 个条目"
        if tool == "search_text":
            query = self._string_arg(args, "query") or "<empty>"
            match_count = metadata.get("match_count")
            if isinstance(match_count, int):
                return f"已搜索 {query!r}，结果{status}；命中 {match_count} 处"
            return f"已搜索 {query!r}，结果{status}；摘要：{output}"
        if tool in {
            "create_directory",
            "write_file",
            "append_text",
            "insert_text",
            "replace_text",
            "replace_lines",
            "patch_file",
        }:
            target = path or "<unknown>"
            return f"已对 {target} 执行 {tool}，结果{status}"
        if tool in {"run_shell", "run_recommended_verification"}:
            rendered = command or "<unknown command>"
            return f"已运行命令 {rendered!r}，结果{status}；摘要：{output}"
        if tool in {"git_status", "git_diff"}:
            return f"已执行 {tool}，结果{status}；摘要：{output}"
        if tool == "file_info" and path:
            return f"已查看 {path} 的文件信息，结果{status}；摘要：{output}"
        if tool == "get_cwd":
            return f"已确认工作区，结果{status}；路径：{output}"
        return f"{tool} {status}；摘要：{output}"

    @staticmethod
    def _string_arg(values: dict[str, Any], key: str) -> str:
        value = values.get(key)
        return value if isinstance(value, str) and value else ""

    @staticmethod
    def _preview(text: str, limit: int = 180) -> str:
        collapsed = " ".join(text.split())
        if len(collapsed) <= limit:
            return collapsed
        return collapsed[:limit] + "..."
