"""暴露给模型使用的本地工具。"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .verification import discover_verification_commands


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
HUNK_HEADER_RE = re.compile(r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@")


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """限制工具输出长度，避免一次观察把模型上下文撑爆。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


@dataclass
class PatchLine:
    """unified diff 中的一行变更。

    op 保存 diff 前缀：空格表示上下文，- 表示删除，+ 表示新增；text 是去掉前缀后的真实文件行。
    """

    op: str
    text: str


@dataclass
class PatchHunk:
    """unified diff 的一个 hunk，记录原文件起点和内部行操作。"""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[PatchLine]


@dataclass
class ToolRegistry:
    """本地工具注册表。

    所有路径型工具都必须经过 _resolve/_resolve_required，确保模型不能逃出 workspace。
    """

    workspace: Path

    def __post_init__(self) -> None:
        self.workspace = self.workspace.resolve()
        # 工具名就是模型在 JSON 动作里填写的 tool 字段，提示词和这里要保持同步。
        self._tools: dict[str, ToolFunc] = {
            "get_cwd": self.get_cwd,
            "list_files": self.list_files,
            "read_file": self.read_file,
            "file_info": self.file_info,
            "search_text": self.search_text,
            "create_directory": self.create_directory,
            "write_file": self.write_file,
            "append_text": self.append_text,
            "insert_text": self.insert_text,
            "replace_text": self.replace_text,
            "replace_lines": self.replace_lines,
            "patch_file": self.patch_file,
            "run_shell": self.run_shell,
            "git_status": self.git_status,
            "git_diff": self.git_diff,
            "discover_verification": self.discover_verification,
            "run_recommended_verification": self.run_recommended_verification,
        }

    def run(self, name: str, args: dict[str, Any]) -> ToolResult:
        """执行工具并把异常统一转换成工具观察。"""
        tool = self._tools.get(name)
        if tool is None:
            return self._result(False, name, "", f"unknown tool: {name}")
        try:
            return tool(args)
        except Exception as exc:  # noqa: BLE001 - 工具异常需要转成观察结果反馈给智能体
            return self._result(False, name, "", f"{type(exc).__name__}: {exc}")

    def get_cwd(self, args: dict[str, Any]) -> ToolResult:
        """返回当前受控工作区路径。"""
        return self._result(True, "get_cwd", str(self.workspace), "")

    def list_files(self, args: dict[str, Any]) -> ToolResult:
        """列出工作区内文件，忽略缓存、依赖和 Git 元数据目录。"""
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
        """读取 UTF-8 文本文件，超出 max_bytes 时只返回前缀。"""
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

    def file_info(self, args: dict[str, Any]) -> ToolResult:
        """返回文件或目录的基础元信息，并同步放进 metadata。"""
        path = self._resolve_required(args, "path")
        rel = self._relative(path)
        info: dict[str, Any] = {
            "path": rel,
            "exists": path.exists(),
        }
        if path.exists():
            info["type"] = "directory" if path.is_dir() else "file"
            if path.is_file():
                info["size_bytes"] = path.stat().st_size
            if path.is_dir():
                info["children"] = len(list(path.iterdir()))
        return self._result(True, "file_info", json.dumps(info, ensure_ascii=False, indent=2), "", info)

    def search_text(self, args: dict[str, Any]) -> ToolResult:
        """在工作区内搜索文本或正则，返回 path/line/text 结构化命中。"""
        query = args.get("query")
        if not isinstance(query, str) or not query:
            raise ValueError("query must be a non-empty string")
        root = self._resolve(args.get("path", "."))
        regex = bool(args.get("regex", False))
        case_sensitive = bool(args.get("case_sensitive", False))
        max_results = int(args.get("max_results", 50))
        max_file_bytes = int(args.get("max_file_bytes", 200_000))
        matcher = self._build_matcher(query, regex, case_sensitive)
        matches: list[dict[str, Any]] = []
        truncated = False

        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in candidates:
            if not path.is_file() or self._is_ignored(path):
                continue
            if path.stat().st_size > max_file_bytes:
                continue
            text = path.read_bytes().decode("utf-8", errors="replace")
            for line_number, line in enumerate(text.splitlines(), 1):
                if not matcher(line):
                    continue
                matches.append(
                    {
                        "path": self._relative(path),
                        "line": line_number,
                        "text": line[:240],
                    }
                )
                if len(matches) >= max_results:
                    truncated = True
                    break
            if truncated:
                break

        metadata = {"matches": matches, "match_count": len(matches), "truncated": truncated}
        output = json.dumps(matches, ensure_ascii=False, indent=2) if matches else "no matches"
        return self._result(True, "search_text", output, "", metadata)

    def write_file(self, args: dict[str, Any]) -> ToolResult:
        """写入完整文件内容；适合创建文件或小文件整体替换。"""
        path = self._resolve_required(args, "path")
        content = args.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return self._result(True, "write_file", f"wrote {self._relative(path)}", "")

    def create_directory(self, args: dict[str, Any]) -> ToolResult:
        """创建目录，适合先搭好文件结构再写入文件。"""
        path = self._resolve_required(args, "path")
        path.mkdir(parents=True, exist_ok=bool(args.get("exist_ok", True)))
        metadata = {"path": self._relative(path), "type": "directory"}
        return self._result(True, "create_directory", f"created directory {metadata['path']}", "", metadata)

    def append_text(self, args: dict[str, Any]) -> ToolResult:
        """向文件末尾追加文本，避免为了加一小段内容重写整个文件。"""
        path = self._resolve_required(args, "path")
        content = args.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        create = bool(args.get("create", True))
        if not path.exists() and not create:
            raise ValueError("file does not exist and create is false")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        metadata = {"path": self._relative(path), "bytes_appended": len(content.encode("utf-8"))}
        return self._result(True, "append_text", f"appended to {metadata['path']}", "", metadata)

    def insert_text(self, args: dict[str, Any]) -> ToolResult:
        """在指定行号前插入文本。

        line 是 1-based 行号；line=1 表示插到文件开头，line=总行数+1 表示追加到末尾。
        """
        path = self._resolve_required(args, "path")
        content = args.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        line = int(args.get("line", 1))
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        if line < 1 or line > len(lines) + 1:
            raise ValueError("line must be between 1 and the number of lines plus 1")
        index = line - 1
        lines[index:index] = [content]
        path.write_text("".join(lines), encoding="utf-8", newline="\n")
        metadata = {"path": self._relative(path), "line": line}
        return self._result(True, "insert_text", f"inserted text into {metadata['path']} at line {line}", "", metadata)

    def replace_text(self, args: dict[str, Any]) -> ToolResult:
        """在单个文件中执行精确文本替换。"""
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

    def replace_lines(self, args: dict[str, Any]) -> ToolResult:
        """替换闭区间行号内的内容，适合精确修改函数或配置片段。

        start/end 都是 1-based，且 end 包含在替换范围内。content 会作为整个区间的新内容。
        """
        path = self._resolve_required(args, "path")
        content = args.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        start = int(args.get("start", 1))
        end = int(args.get("end", start))
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        if start < 1 or end < start or end > len(lines):
            raise ValueError("start/end must select an existing inclusive line range")
        lines[start - 1 : end] = [content]
        path.write_text("".join(lines), encoding="utf-8", newline="\n")
        metadata = {"path": self._relative(path), "start": start, "end": end}
        return self._result(
            True,
            "replace_lines",
            f"replaced lines {start}-{end} in {metadata['path']}",
            "",
            metadata,
        )

    def patch_file(self, args: dict[str, Any]) -> ToolResult:
        """把单文件 unified diff 应用到目标文件。

        这个工具适合较复杂但仍局限在一个文件内的改动：模型可以先 read_file，再生成标准
        unified diff。解析器会校验 hunk 上下文，避免文件内容已经变化时误改到错误位置。
        """
        path = self._resolve_required(args, "path")
        patch = args.get("patch")
        if not isinstance(patch, str) or not patch.strip():
            raise ValueError("patch must be a non-empty string")
        if not path.is_file():
            raise ValueError("patch_file only supports existing text files")

        rel_path = self._relative(path)
        hunks = self._parse_unified_patch(patch, rel_path)
        original_lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        updated_lines, added_lines, removed_lines = self._apply_patch_hunks(original_lines, hunks)
        path.write_text("".join(updated_lines), encoding="utf-8", newline="\n")

        metadata = {
            "path": rel_path,
            "hunk_count": len(hunks),
            "added_lines": added_lines,
            "removed_lines": removed_lines,
        }
        return self._result(
            True,
            "patch_file",
            f"applied {len(hunks)} patch hunk(s) to {rel_path}",
            "",
            metadata,
        )

    def run_shell(self, args: dict[str, Any]) -> ToolResult:
        """执行普通 shell 命令，并返回 stdout/stderr/returncode metadata。"""
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        timeout = int(args.get("timeout", 30))
        return self._run_command("run_shell", command, timeout)

    def git_status(self, args: dict[str, Any]) -> ToolResult:
        """用 git status --short 查看工作区变更。"""
        completed = self._run_git(["status", "--short"])
        output = completed.stdout or "clean"
        return self._result(
            completed.returncode == 0,
            "git_status",
            _truncate(output),
            "" if completed.returncode == 0 else _truncate(completed.stderr),
            {"returncode": completed.returncode},
        )

    def git_diff(self, args: dict[str, Any]) -> ToolResult:
        """查看 Git diff；可选限制到单个工作区内路径。"""
        command = ["diff"]
        if bool(args.get("staged", False)):
            command.append("--cached")
        if args.get("path"):
            path = self._resolve_required(args, "path")
            command.extend(["--", self._relative(path)])
        completed = self._run_git(command)
        output = completed.stdout or "no diff"
        return self._result(
            completed.returncode == 0,
            "git_diff",
            _truncate(output, int(args.get("max_chars", MAX_OUTPUT_CHARS))),
            "" if completed.returncode == 0 else _truncate(completed.stderr),
            {"returncode": completed.returncode, "staged": bool(args.get("staged", False))},
        )

    def discover_verification(self, args: dict[str, Any]) -> ToolResult:
        """暴露 harness 推荐验证命令，方便模型主动选择验证方式。"""
        commands = discover_verification_commands(self.workspace)
        metadata = {"commands": [command.to_dict() for command in commands]}
        if not commands:
            return self._result(True, "discover_verification", "no recommended verification command", "", metadata)
        output = "\n".join(f"{index}. {command.to_prompt()}" for index, command in enumerate(commands, 1))
        return self._result(True, "discover_verification", output, "", metadata)

    def run_recommended_verification(self, args: dict[str, Any]) -> ToolResult:
        """运行 discover_verification 返回的第 index 条推荐验证命令。"""
        commands = discover_verification_commands(self.workspace)
        if not commands:
            return self._result(False, "run_recommended_verification", "", "no recommended verification command")
        index = int(args.get("index", 1))
        if index < 1 or index > len(commands):
            raise ValueError("index must select an available recommended command")
        command = commands[index - 1]
        timeout = int(args.get("timeout", 120))
        return self._run_command(
            "run_recommended_verification",
            command.command,
            timeout,
            {"recommended_verification": True, **command.to_dict()},
        )

    def _parse_unified_patch(self, patch: str, expected_path: str) -> list[PatchHunk]:
        """解析单文件 unified diff，并校验可选文件头是否指向目标 path。"""
        patch_lines = patch.splitlines(keepends=True)
        hunks: list[PatchHunk] = []
        index = 0

        while index < len(patch_lines):
            line = patch_lines[index]
            if line.startswith(("--- ", "+++ ")):
                self._validate_patch_header_path(line, expected_path)
                index += 1
                continue
            if not line.startswith("@@ "):
                index += 1
                continue

            match = HUNK_HEADER_RE.match(line)
            if match is None:
                raise ValueError(f"invalid patch hunk header: {line.rstrip()}")
            hunk = PatchHunk(
                old_start=int(match.group("old_start")),
                old_count=int(match.group("old_count") or "1"),
                new_start=int(match.group("new_start")),
                new_count=int(match.group("new_count") or "1"),
                lines=[],
            )
            index += 1

            # hunk 内容只接受 unified diff 的三种前缀；no-newline 标记只描述上一行，不参与应用。
            while index < len(patch_lines) and not patch_lines[index].startswith("@@ "):
                hunk_line = patch_lines[index]
                if hunk_line.startswith("\\ No newline at end of file"):
                    index += 1
                    continue
                if not hunk_line:
                    raise ValueError("invalid empty patch line")
                op = hunk_line[0]
                if op not in {" ", "-", "+"}:
                    break
                hunk.lines.append(PatchLine(op, hunk_line[1:]))
                index += 1

            if not hunk.lines:
                raise ValueError("patch hunk has no body")
            self._validate_hunk_counts(hunk)
            hunks.append(hunk)

        if not hunks:
            raise ValueError("patch must contain at least one unified diff hunk")
        return hunks

    def _validate_patch_header_path(self, line: str, expected_path: str) -> None:
        """校验 diff 文件头，防止模型把另一个文件的 patch 应用到当前 path。"""
        raw_path = line[4:].strip().split("\t", 1)[0].split(" ", 1)[0]
        if raw_path == "/dev/null":
            return
        normalized = raw_path.replace("\\", "/")
        if normalized.startswith(("a/", "b/")):
            normalized = normalized[2:]
        if normalized != expected_path:
            raise ValueError(f"patch header path {raw_path!r} does not match target path {expected_path!r}")

    def _validate_hunk_counts(self, hunk: PatchHunk) -> None:
        """核对 hunk 头部声明的行数，尽早发现模型生成的不完整 patch。"""
        old_count = sum(1 for line in hunk.lines if line.op in {" ", "-"})
        new_count = sum(1 for line in hunk.lines if line.op in {" ", "+"})
        if old_count != hunk.old_count or new_count != hunk.new_count:
            raise ValueError(
                "patch hunk line counts do not match header "
                f"(old {old_count}/{hunk.old_count}, new {new_count}/{hunk.new_count})"
            )

    def _apply_patch_hunks(self, original_lines: list[str], hunks: list[PatchHunk]) -> tuple[list[str], int, int]:
        """顺序应用 hunk；上下文或删除行不匹配时直接失败，避免静默错改。"""
        current_lines = list(original_lines)
        line_offset = 0
        added_lines = 0
        removed_lines = 0

        for hunk in hunks:
            target_index = hunk.old_start - 1 + line_offset
            if target_index < 0 or target_index > len(current_lines):
                raise ValueError(f"patch hunk starts outside file at original line {hunk.old_start}")

            read_index = target_index
            replacement: list[str] = []
            for patch_line in hunk.lines:
                if patch_line.op == "+":
                    replacement.append(patch_line.text)
                    added_lines += 1
                    continue

                if read_index >= len(current_lines):
                    raise ValueError(f"patch context mismatch near original line {hunk.old_start}")
                actual = current_lines[read_index]
                if not self._patch_lines_match(actual, patch_line.text):
                    raise ValueError(
                        "patch context mismatch near "
                        f"line {read_index + 1}: expected {patch_line.text.rstrip()!r}, got {actual.rstrip()!r}"
                    )

                if patch_line.op == " ":
                    replacement.append(actual)
                elif patch_line.op == "-":
                    removed_lines += 1
                read_index += 1

            consumed = read_index - target_index
            current_lines[target_index:read_index] = replacement
            line_offset += len(replacement) - consumed

        return current_lines, added_lines, removed_lines

    @staticmethod
    def _patch_lines_match(actual: str, expected: str) -> bool:
        """比较 patch 行和文件行；兼容 CRLF/LF 差异，但不放宽实际文本内容。"""
        return actual == expected or actual.rstrip("\r\n") == expected.rstrip("\r\n")

    def _run_command(
        self,
        tool: str,
        command: str,
        timeout: int,
        metadata: dict[str, Any] | None = None,
    ) -> ToolResult:
        """执行 shell 命令的共享实现。

        run_shell 和 run_recommended_verification 都走这里，保证危险命令检查和 metadata 形状一致。
        """
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
        result_metadata = {
            "command": command,
            "returncode": completed.returncode,
            "stdout": _truncate(completed.stdout),
            "stderr": _truncate(completed.stderr),
        }
        if metadata:
            result_metadata.update(metadata)
        return self._result(ok, tool, _truncate(output), "" if ok else f"exit {completed.returncode}", result_metadata)

    def _run_git(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        """用非 shell 方式运行 Git，避免把路径或参数拼成命令字符串。"""
        return subprocess.run(
            ["git", *args],
            cwd=self.workspace,
            shell=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )

    def _resolve_required(self, args: dict[str, Any], key: str) -> Path:
        """读取必填路径参数并解析到 workspace 内。"""
        value = args.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{key} must be a non-empty string")
        return self._resolve(value)

    def _resolve(self, value: Any) -> Path:
        """解析模型传入路径，并拒绝越过 workspace 的路径。"""
        if not isinstance(value, str) or not value:
            raise ValueError("path must be a non-empty string")
        path = (self.workspace / value).resolve()
        if path != self.workspace and self.workspace not in path.parents:
            raise ValueError(f"path escapes workspace: {value}")
        return path

    def _relative(self, path: Path) -> str:
        """把绝对路径转成统一的 POSIX 风格相对路径。"""
        return str(path.relative_to(self.workspace)).replace("\\", "/")

    def _is_ignored(self, path: Path) -> bool:
        """判断搜索和列文件时是否应忽略该路径。"""
        rel = path.relative_to(self.workspace)
        return any(part in IGNORED_DIRS for part in rel.parts)

    def _build_matcher(
        self,
        query: str,
        regex: bool,
        case_sensitive: bool,
    ) -> Callable[[str], bool]:
        """构造 search_text 使用的行匹配函数。"""
        if regex:
            flags = 0 if case_sensitive else re.IGNORECASE
            pattern = re.compile(query, flags)
            return lambda line: bool(pattern.search(line))

        needle = query if case_sensitive else query.lower()
        return lambda line: needle in (line if case_sensitive else line.lower())

    def _check_command(self, command: str) -> None:
        """拦截明显危险命令；需要时可通过环境变量显式放开。"""
        if os.getenv("SMALLAGENT_ALLOW_DANGEROUS") == "1":
            return
        normalized = f" {command.lower()} "
        if any(marker in normalized for marker in DANGEROUS_COMMAND_MARKERS):
            raise ValueError("dangerous command blocked; set SMALLAGENT_ALLOW_DANGEROUS=1 to override")

    @staticmethod
    def _result(
        ok: bool,
        tool: str,
        output: str,
        error: str,
        metadata: dict[str, Any] | None = None,
    ) -> ToolResult:
        """统一工具结果结构；metadata 用于机器可读证据。"""
        result: ToolResult = {"ok": ok, "tool": tool, "output": output, "error": error}
        if metadata is not None:
            result["metadata"] = metadata
        return result
