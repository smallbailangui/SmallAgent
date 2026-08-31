"""项目验证命令发现。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class VerificationCommand:
    """基于项目形状发现的推荐验证命令。"""

    command: str
    reason: str

    def to_prompt(self) -> str:
        return f"{self.command}（{self.reason}）"

    def to_dict(self) -> dict[str, str]:
        return {
            "command": self.command,
            "reason": self.reason,
        }


def discover_verification_commands(workspace: Path | None) -> list[VerificationCommand]:
    """根据常见项目文件发现推荐验证命令。

    这份逻辑被 tools 和 completion 共享：模型可以通过工具看到推荐命令，
    harness 也可以用同一套规则判断推荐命令是否被成功执行。
    """
    if workspace is None or not workspace.exists():
        return []
    commands: list[VerificationCommand] = []
    # 项目自带检查脚本优先级最高，因为它通常封装了作者希望运行的完整检查。
    if (workspace / "scripts" / "check.ps1").exists():
        commands.append(
            VerificationCommand(
                "powershell -ExecutionPolicy Bypass -File scripts/check.ps1",
                "检测到 scripts/check.ps1",
            )
        )
    # 没有项目脚本时，按常见生态文件给出保守默认命令。
    if (workspace / "pyproject.toml").exists() and (workspace / "tests").exists():
        commands.append(
            VerificationCommand(
                "python -m unittest discover -s tests -v",
                "检测到 pyproject.toml 和 tests 目录",
            )
        )
    if (workspace / "package.json").exists():
        commands.append(VerificationCommand("npm test", "检测到 package.json"))
    if (workspace / "Cargo.toml").exists():
        commands.append(VerificationCommand("cargo test", "检测到 Cargo.toml"))
    if (workspace / "go.mod").exists():
        commands.append(VerificationCommand("go test ./...", "检测到 go.mod"))
    return commands


def normalize_command(command: str) -> str:
    """把命令归一化，用于比较模型给出的验证命令。

    这里会统一 Windows 反斜杠并压缩空白，避免同一命令因为路径分隔符不同而匹配失败。
    """
    normalized = command.replace("\\", "/")
    return " ".join(normalized.strip().lower().split())
