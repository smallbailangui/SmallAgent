"""Local tools exposed to the model."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


ToolResult = dict[str, Any]
ToolFunc = Callable[[dict[str, Any]], ToolResult]


MAX_OUTPUT_CHARS = 12_000
IGNORED_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"}
DANGEROUS_COMMAND_MARKERS = (
    " rm ",
    "rm -",
    "del ",
    "erase ",
    "rmdir ",
    "remove-item",
    "format ",
    "shutdown",
    "git reset --hard",
    "git clean",
)


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


@dataclass
class ToolRegistry:
    workspace: Path

    def __post_init__(self) -> None:
        self.workspace = self.workspace.resolve()
        self._tools: dict[str, ToolFunc] = {
            "get_cwd": self.get_cwd,
            "list_files": self.list_files,
            "read_file": self.read_file,
            "write_file": self.write_file,
            "replace_text": self.replace_text,
            "run_shell": self.run_shell,
        }

    def run(self, name: str, args: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return self._result(False, name, "", f"unknown tool: {name}")
        try:
            return tool(args)
        except Exception as exc:  # noqa: BLE001 - tool errors must be returned to the agent
            return self._result(False, name, "", f"{type(exc).__name__}: {exc}")

    def get_cwd(self, args: dict[str, Any]) -> ToolResult:
        return self._result(True, "get_cwd", str(self.workspace), "")

    def list_files(self, args: dict[str, Any]) -> ToolResult:
        root = self._resolve(args.get("path", "."))
        max_depth = int(args.get("max_depth", 3))
        lines: list[str] = []

        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if len(rel.parts) > max_depth:
                continue
            if any(part in IGNORED_DIRS for part in rel.parts):
                continue
            suffix = "/" if path.is_dir() else ""
            lines.append(str(path.relative_to(self.workspace)).replace("\\", "/") + suffix)

        return self._result(True, "list_files", "\n".join(lines), "")

    def read_file(self, args: dict[str, Any]) -> ToolResult:
        path = self._resolve_required(args, "path")
        max_bytes = int(args.get("max_bytes", 120_000))
        data = path.read_bytes()
        if len(data) > max_bytes:
            data = data[:max_bytes]
            suffix = f"\n...[truncated to {max_bytes} bytes]".encode("utf-8")
        else:
            suffix = b""
        text = data.decode("utf-8", errors="replace") + suffix.decode("utf-8")
        return self._result(True, "read_file", text, "")

    def write_file(self, args: dict[str, Any]) -> ToolResult:
        path = self._resolve_required(args, "path")
        content = args.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return self._result(True, "write_file", f"wrote {path.relative_to(self.workspace)}", "")

    def replace_text(self, args: dict[str, Any]) -> ToolResult:
        path = self._resolve_required(args, "path")
        old = args.get("old")
        new = args.get("new")
        count = int(args.get("count", 1))
        if not isinstance(old, str) or not isinstance(new, str):
            raise ValueError("old and new must be strings")

        text = path.read_text(encoding="utf-8")
        if old not in text:
            raise ValueError("old text not found")
        updated = text.replace(old, new, count)
        path.write_text(updated, encoding="utf-8", newline="\n")
        changed = text.count(old) if count < 0 else min(text.count(old), count)
        return self._result(True, "replace_text", f"replaced {changed} occurrence(s)", "")

    def run_shell(self, args: dict[str, Any]) -> ToolResult:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        timeout = int(args.get("timeout", 30))
        self._check_command(command)

        completed = subprocess.run(
            command,
            cwd=self.workspace,
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        output = completed.stdout
        if completed.stderr:
            output += ("\n" if output else "") + completed.stderr
        ok = completed.returncode == 0
        return self._result(ok, "run_shell", _truncate(output), "" if ok else f"exit {completed.returncode}")

    def _resolve_required(self, args: dict[str, Any], key: str) -> Path:
        value = args.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{key} must be a non-empty string")
        return self._resolve(value)

    def _resolve(self, value: Any) -> Path:
        if not isinstance(value, str) or not value:
            raise ValueError("path must be a non-empty string")
        path = (self.workspace / value).resolve()
        if path != self.workspace and self.workspace not in path.parents:
            raise ValueError(f"path escapes workspace: {value}")
        return path

    def _check_command(self, command: str) -> None:
        if os.getenv("SMALLAGENT_ALLOW_DANGEROUS") == "1":
            return
        normalized = f" {command.lower()} "
        if any(marker in normalized for marker in DANGEROUS_COMMAND_MARKERS):
            raise ValueError("dangerous command blocked; set SMALLAGENT_ALLOW_DANGEROUS=1 to override")

    @staticmethod
    def _result(ok: bool, tool: str, output: str, error: str) -> ToolResult:
        return {"ok": ok, "tool": tool, "output": output, "error": error}
