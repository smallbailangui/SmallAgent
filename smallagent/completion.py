"""完成度检查：在最终回答前做轻量 harness 校验。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .verification import VerificationCommand, discover_verification_commands, normalize_command


# 这三组工具分类是 harness 的证据标签来源。
# 一个工具可以属于多个类别，例如 run_shell 既能提供上下文，也能提供验证证据。
MODIFYING_TOOLS = {"create_directory", "write_file", "append_text", "insert_text", "replace_text", "replace_lines"}
VERIFICATION_TOOLS = {"read_file", "list_files", "run_shell", "run_recommended_verification"}
CONTEXT_TOOLS = {
    "get_cwd",
    "list_files",
    "read_file",
    "file_info",
    "search_text",
    "git_status",
    "git_diff",
    "discover_verification",
    "run_shell",
}
MUTATION_KEYWORDS = (
    "创建",
    "写",
    "新增",
    "添加",
    "修改",
    "修复",
    "实现",
    "完善",
    "更新",
    "create",
    "write",
    "add",
    "modify",
    "fix",
    "implement",
    "update",
)
TEST_KEYWORDS = (
    "测试",
    "检查",
    "验证",
    "test",
    "check",
    "verify",
    "lint",
)
# TEST_KEYWORDS 用于从任务文本生成验收标准；TEST_CLAIM_KEYWORDS 用于检查 final 里的测试声明。
# 后者刻意不包含“验证/verify”，避免“我已复查文件”这类普通验证被误判成测试通过声明。
TEST_CLAIM_KEYWORDS = ("测试", "检查", "test", "check", "lint")
SNAPSHOT_IGNORED_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"}
SNAPSHOT_MAX_BYTES = 1_000_000


@dataclass
class AcceptanceCriterion:
    """一条可由工具轨迹检查的任务验收标准。"""

    key: str
    description: str

    def to_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "description": self.description,
        }


@dataclass
class ChangedFile:
    """一次任务运行前后检测到的工作区文件变化。"""

    path: str
    status: str

    def to_prompt(self) -> str:
        return f"{self.status}: {self.path}"

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "status": self.status,
        }


@dataclass
class Evidence:
    """一条从工具结果提炼出的验收证据。

    tool_results 是原始工具观察；Evidence 是给完成度检查和 JSON report 用的稳定结构。
    tags 用来表达这条证据能满足哪些验收标准，details 保留 path/command 等可追溯信息。
    """

    index: int
    tool: str
    ok: bool
    tags: set[str] = field(default_factory=set)
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_prompt(self) -> str:
        status = "成功" if self.ok else "失败"
        tags = ",".join(sorted(self.tags)) if self.tags else "uncategorized"
        if self.summary:
            return f"{self.tool} {status} [{tags}]：{self.summary}"
        return f"{self.tool} {status} [{tags}]"

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "tool": self.tool,
            "ok": self.ok,
            "tags": sorted(self.tags),
            "summary": self.summary,
            "details": dict(self.details),
        }


@dataclass
class CriterionResult:
    """一条验收标准的机器可读评估结果。

    evidence 保存命中的证据对象；导出 JSON 时只写 evidence_indices，避免重复塞大量摘要。
    """

    criterion: AcceptanceCriterion
    met: bool
    evidence: list[Evidence] = field(default_factory=list)

    def to_prompt(self) -> str:
        status = "已满足" if self.met else "未满足"
        if not self.evidence:
            return f"{status}：{self.criterion.description}"
        evidence_refs = ", ".join(f"#{item.index}" for item in self.evidence)
        return f"{status}：{self.criterion.description}（证据：{evidence_refs}）"

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.criterion.to_dict(),
            "met": self.met,
            "evidence_indices": [item.index for item in self.evidence],
        }


@dataclass
class CompletionCheck:
    """一次最终回答是否可接受的判断。

    criteria_status/evidence_summary 面向模型和人阅读；criteria_results/evidence/changed_files
    面向外部考核脚本稳定解析。
    """

    accepted: bool
    reasons: list[str] = field(default_factory=list)
    criteria_status: list[str] = field(default_factory=list)
    evidence_summary: list[str] = field(default_factory=list)
    criteria_results: list[CriterionResult] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    changed_files: list[ChangedFile] = field(default_factory=list)
    verification_commands: list[VerificationCommand] = field(default_factory=list)

    def to_prompt(self) -> str:
        status = "通过" if self.accepted else "未通过"
        lines = [f"完成度检查：{status}。"]
        if self.criteria_status:
            lines.append("验收标准：")
            lines.extend(f"- {item}" for item in self.criteria_status)
        if self.evidence_summary:
            lines.append("最近证据：")
            lines.extend(f"- {item}" for item in self.evidence_summary)
        if self.changed_files:
            lines.append("文件变化：")
            lines.extend(f"- {item.to_prompt()}" for item in self.changed_files[:20])
        if self.reasons:
            lines.append("阻塞原因：")
            lines.extend(f"- {reason}" for reason in self.reasons)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "criteria_status": list(self.criteria_status),
            "evidence_summary": list(self.evidence_summary),
            "criteria_results": [result.to_dict() for result in self.criteria_results],
            "evidence": [item.to_dict() for item in self.evidence],
            "changed_files": [item.to_dict() for item in self.changed_files],
            "verification_commands": [item.to_dict() for item in self.verification_commands],
        }

    def missing_criteria_keys(self) -> set[str]:
        return {
            result.criterion.key
            for result in self.criteria_results
            if not result.met
        }


class CompletionHarness:
    """根据任务、工具轨迹和工作区变化判断 agent 是否有足够证据结束任务。"""

    def __init__(self) -> None:
        self.tool_results: list[dict[str, Any]] = []
        self.evidence: list[Evidence] = []
        self.criteria: list[AcceptanceCriterion] = []
        self.workspace: Path | None = None
        self.initial_snapshot: dict[str, str] = {}
        self.verification_commands: list[VerificationCommand] = []

    def start_task(self, task: str, workspace: Path | None = None) -> None:
        """开启一次任务级检查，重置证据并拍摄初始工作区快照。"""
        self.tool_results = []
        self.evidence = []
        self.workspace = workspace.resolve() if workspace else None
        self.initial_snapshot = self._snapshot_workspace(self.workspace) if self.workspace else {}
        self.verification_commands = discover_verification_commands(self.workspace)
        self.criteria = self._build_acceptance_criteria(task)

    def record_tool_call(
        self,
        tool: str,
        args: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """记录带参数的工具调用，用于生成可追溯 Evidence。"""
        copied = dict(result)
        copied.setdefault("tool", tool)
        self.tool_results.append(copied)
        self.evidence.append(self._evidence_from_result(copied, args))

    def record_tool_result(self, result: dict[str, Any]) -> None:
        """兼容旧测试和简单调用；没有 args 时仍然保留基础工具证据。"""
        copied = dict(result)
        self.tool_results.append(copied)
        self.evidence.append(self._evidence_from_result(copied, {}))

    def to_prompt(self) -> str:
        """渲染给模型看的验收上下文，让模型提前知道要补哪些证据。"""
        if not self.criteria:
            return "验收标准：暂无。"
        lines = ["验收标准："]
        for index, criterion in enumerate(self.criteria, 1):
            lines.append(f"{index}. {criterion.description}")
        if self.verification_commands:
            lines.append("推荐验证命令：")
            for command in self.verification_commands:
                lines.append(f"- {command.to_prompt()}")
        if self.evidence:
            lines.append("最近证据：")
            for evidence in self.evidence[-5:]:
                lines.append(f"- {evidence.to_prompt()}")
        return "\n".join(lines)

    def evaluate(self, final_message: str) -> CompletionCheck:
        """评估 final 是否可以被接受。

        这个方法只做确定性检查：final 文本、最近失败、修改后验证、测试声明和验收标准。
        如果不通过，agent 会把原因作为 SELF_CHECK 反馈给模型继续工作。
        """
        reasons: list[str] = []
        criteria_status: list[str] = []
        criteria_results: list[CriterionResult] = []
        evidence_summary = [evidence.to_prompt() for evidence in self.evidence[-5:]]
        changed_files = self.changed_files()
        if not final_message.strip():
            reasons.append("最终回答不能为空，需要说明完成了什么和如何验证。")

        if not self.evidence:
            reasons.append("缺少工具观察结果，需先收集工作区证据再结束。")

        if self.evidence and not self.evidence[-1].ok:
            reasons.append(f"最近一次工具 {self.evidence[-1].tool} 失败，需先修复或收集新的成功证据。")

        last_mutation_index = self._last_successful_mutation_index()
        if last_mutation_index is not None and not self._has_verification_after(last_mutation_index):
            reasons.append("检测到文件修改后缺少验证证据，需读取关键文件或运行检查命令。")

        if self._mentions_tests_passed(final_message) and not self._has_successful_evidence("shell"):
            reasons.append("最终回答声称测试或检查通过，但缺少成功的 shell 验证证据。")

        for criterion in self.criteria:
            matched_evidence = self._matching_evidence(criterion)
            result = CriterionResult(criterion=criterion, met=bool(matched_evidence), evidence=matched_evidence)
            criteria_results.append(result)
            criteria_status.append(result.to_prompt())
            if not result.met:
                reasons.append(f"验收标准未满足：{criterion.description}")

        return CompletionCheck(
            accepted=not reasons,
            reasons=reasons,
            criteria_status=criteria_status,
            evidence_summary=evidence_summary,
            criteria_results=criteria_results,
            evidence=list(self.evidence),
            changed_files=changed_files,
            verification_commands=list(self.verification_commands),
        )

    def changed_files(self) -> list[ChangedFile]:
        """对比任务开始时的快照和当前快照，生成轻量文件变更列表。"""
        if self.workspace is None:
            return []
        current_snapshot = self._snapshot_workspace(self.workspace)
        paths = sorted(set(self.initial_snapshot) | set(current_snapshot))
        changes: list[ChangedFile] = []
        for path in paths:
            before = self.initial_snapshot.get(path)
            after = current_snapshot.get(path)
            if before is None and after is not None:
                changes.append(ChangedFile(path, "added"))
            elif before is not None and after is None:
                changes.append(ChangedFile(path, "deleted"))
            elif before != after:
                changes.append(ChangedFile(path, "modified"))
        return changes

    def next_recommended_verification(self) -> VerificationCommand | None:
        """返回还没有成功执行过的首个推荐验证命令。"""
        if not self.verification_commands:
            return None
        if self._has_successful_evidence("recommended_verification"):
            return None
        return self.verification_commands[0]

    def _evidence_from_result(self, result: dict[str, Any], args: dict[str, Any]) -> Evidence:
        """把原始工具结果和调用参数压缩成可评分证据。"""
        tool = str(result.get("tool", "unknown"))
        ok = bool(result.get("ok", False))
        output = str(result.get("output", "") or result.get("error", ""))
        tags = self._tags_for_tool(tool, ok, args, result)
        return Evidence(
            index=len(self.evidence) + 1,
            tool=tool,
            ok=ok,
            tags=tags,
            summary=output[:160],
            details=self._details_for_tool(tool, args, result),
        )

    def _tags_for_tool(
        self,
        tool: str,
        ok: bool,
        args: dict[str, Any],
        result: dict[str, Any],
    ) -> set[str]:
        """根据工具类型和 metadata 给 Evidence 打标签。"""
        if not ok:
            return {"failure"}
        tags: set[str] = set()
        if tool in CONTEXT_TOOLS:
            tags.add("context")
        if tool in MODIFYING_TOOLS:
            tags.add("mutation")
        if tool in VERIFICATION_TOOLS:
            tags.add("verification")
        if tool in {"run_shell", "run_recommended_verification"}:
            tags.add("shell")
            command = str(args.get("command", ""))
            metadata = result.get("metadata", {})
            is_recommended = isinstance(metadata, dict) and bool(metadata.get("recommended_verification"))
            if is_recommended or self._is_recommended_verification_command(command):
                tags.add("recommended_verification")
        return tags or {"tool"}

    def _details_for_tool(
        self,
        tool: str,
        args: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """提取 report 中需要稳定保留的调用细节。"""
        details: dict[str, Any] = {}
        if tool in {
            "read_file",
            "create_directory",
            "write_file",
            "append_text",
            "insert_text",
            "replace_text",
            "replace_lines",
            "file_info",
        } and isinstance(args.get("path"), str):
            details["path"] = args["path"]
        if tool in {"insert_text"} and isinstance(args.get("line"), int):
            details["line"] = args["line"]
        if tool in {"replace_lines"}:
            if isinstance(args.get("start"), int):
                details["start"] = args["start"]
            if isinstance(args.get("end"), int):
                details["end"] = args["end"]
        if tool in {"list_files", "git_diff"} and isinstance(args.get("path", "."), str):
            details["path"] = args.get("path", ".")
        if tool == "search_text" and isinstance(args.get("query"), str):
            details["query"] = args["query"]
            details["path"] = args.get("path", ".")
        if tool in {"run_shell", "run_recommended_verification"} and isinstance(args.get("command"), str):
            details["command"] = args["command"]
        metadata = result.get("metadata", {})
        if isinstance(metadata, dict):
            for key in ("command", "returncode", "reason", "recommended_verification"):
                if key in metadata:
                    details[key] = metadata[key]
        return details

    def _build_acceptance_criteria(self, task: str) -> list[AcceptanceCriterion]:
        """从任务文本生成保守验收标准。

        当前实现只用关键词启发式，宁可少生成一点，也避免把无关任务误拦死。
        """
        normalized = task.lower()
        criteria = [
            AcceptanceCriterion("context", "已通过工具收集任务相关工作区上下文。"),
        ]
        expects_mutation = any(keyword in normalized for keyword in MUTATION_KEYWORDS)
        expects_test = any(keyword in normalized for keyword in TEST_KEYWORDS)

        if expects_mutation:
            criteria.append(AcceptanceCriterion("mutation", "如果任务要求改动，已有成功的文件修改证据。"))
            if self.workspace is not None:
                criteria.append(AcceptanceCriterion("workspace_change", "工作区快照检测到任务期间的文件变化。"))
            criteria.append(AcceptanceCriterion("verification", "文件修改后已有成功的复查或验证证据。"))
        elif expects_test:
            criteria.append(AcceptanceCriterion("verification", "已有成功的检查或验证证据。"))

        if expects_test:
            criteria.append(AcceptanceCriterion("run_shell", "任务提到测试或检查时，已成功运行 shell 验证命令。"))
        if (expects_mutation or expects_test) and self.verification_commands:
            criteria.append(AcceptanceCriterion("recommended_verification", "已成功运行 harness 推荐的项目验证命令。"))

        return criteria

    def _criterion_met(self, criterion: AcceptanceCriterion) -> bool:
        return bool(self._matching_evidence(criterion))

    def _matching_evidence(self, criterion: AcceptanceCriterion) -> list[Evidence]:
        """为单条验收标准找能支撑它的证据。"""
        if criterion.key == "context":
            return self._successful_evidence("context")
        if criterion.key == "mutation":
            return self._successful_evidence("mutation")
        if criterion.key == "workspace_change":
            if self.changed_files():
                return self._successful_evidence("mutation")
            return []
        if criterion.key == "verification":
            last_mutation_index = self._last_successful_mutation_index()
            if last_mutation_index is None:
                return self._successful_evidence("verification")
            return self._verification_after(last_mutation_index)
        if criterion.key == "run_shell":
            return self._successful_evidence("shell")
        if criterion.key == "recommended_verification":
            return self._successful_evidence("recommended_verification")
        return []

    def _has_successful_evidence(self, tag: str) -> bool:
        return bool(self._successful_evidence(tag))

    def _successful_evidence(self, tag: str) -> list[Evidence]:
        return [evidence for evidence in self.evidence if evidence.ok and tag in evidence.tags]

    def _last_successful_mutation_index(self) -> int | None:
        for index in range(len(self.evidence) - 1, -1, -1):
            evidence = self.evidence[index]
            if evidence.ok and "mutation" in evidence.tags:
                return index
        return None

    def _has_verification_after(self, mutation_index: int) -> bool:
        return bool(self._verification_after(mutation_index))

    def _verification_after(self, mutation_index: int) -> list[Evidence]:
        return [
            evidence
            for evidence in self.evidence[mutation_index + 1 :]
            if evidence.ok and "verification" in evidence.tags
        ]

    def _snapshot_workspace(self, workspace: Path | None) -> dict[str, str]:
        """生成工作区快照。

        小文件用 sha256 判断内容是否变化；大文件只记录大小，避免为了快照消耗太多内存。
        """
        if workspace is None or not workspace.exists():
            return {}
        snapshot: dict[str, str] = {}
        for path in sorted(workspace.rglob("*")):
            rel = path.relative_to(workspace)
            if any(part in SNAPSHOT_IGNORED_DIRS for part in rel.parts):
                continue
            if path.is_dir():
                snapshot[str(rel).replace("\\", "/") + "/"] = "dir"
                continue
            if not path.is_file():
                continue
            if path.stat().st_size > SNAPSHOT_MAX_BYTES:
                digest = f"large:{path.stat().st_size}"
            else:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            snapshot[str(rel).replace("\\", "/")] = digest
        return snapshot

    def _is_recommended_verification_command(self, command: str) -> bool:
        """判断模型手写的 shell 命令是否等价于 harness 推荐命令。"""
        normalized = normalize_command(command)
        return any(normalized == normalize_command(item.command) for item in self.verification_commands)

    def _mentions_tests_passed(self, final_message: str) -> bool:
        """检查 final 是否声称测试/检查已经通过。"""
        normalized = final_message.lower()
        pass_markers = ("通过", "passed", "pass", "ok", "成功")
        return any(keyword in normalized for keyword in TEST_CLAIM_KEYWORDS) and any(
            marker in normalized for marker in pass_markers
        )
