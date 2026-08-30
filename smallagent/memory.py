"""记忆层：保存任务过程中的短期记忆。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MemoryItem:
    """一条可复述的运行记忆。"""

    kind: str
    content: str


@dataclass
class ShortTermMemory:
    """当前任务内的短期记忆，后续可替换为文件或向量存储。"""

    items: list[MemoryItem] = field(default_factory=list)
    limit: int = 20

    def remember(self, kind: str, content: str) -> None:
        self.items.append(MemoryItem(kind=kind, content=content))
        if len(self.items) > self.limit:
            self.items = self.items[-self.limit :]

    def remember_tool_result(self, result: dict[str, Any]) -> None:
        tool = str(result.get("tool", ""))
        ok = bool(result.get("ok", False))
        output = str(result.get("output", "") or result.get("error", ""))
        self.remember("tool", f"{tool} {'成功' if ok else '失败'}：{output[:300]}")

    def to_prompt(self) -> str:
        if not self.items:
            return "短期记忆：暂无。"
        lines = ["短期记忆："]
        for item in self.items[-8:]:
            lines.append(f"- [{item.kind}] {item.content}")
        return "\n".join(lines)
