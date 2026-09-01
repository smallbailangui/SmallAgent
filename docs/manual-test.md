# SmallAgent 手动测试清单

这个文件用于真实验证终端 agent 是否可用。建议先在当前仓库跑只读任务，再到临时目录测试写文件，避免一开始就改动核心代码。

## 准备

1. 确认 `.env` 使用纯文本键值：

```text
OPENAI_API_KEY=你的 key
SMALLAGENT_MODEL=你的模型名
OPENAI_BASE_URL=https://api.deepseek.com
```

也可以使用 `BASE_URL` 作为 `OPENAI_BASE_URL` 的兼容别名，但推荐优先使用 `OPENAI_BASE_URL`。

2. 运行自动测试：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```

3. 启动交互模式：

```powershell
python -m smallagent --interactive --history-file tmp/interactive-history.json --report-file tmp/interactive-report.json
```

## 测试 1：项目理解

输入：

```text
当前项目是做了什么？请先查看 README 和 docs/design.md 再回答
```

预期：

- agent 应先调用文件查看类工具，例如 `list_files`、`read_file` 或 `search_text`。
- final 应能说明它是不依赖 agent 框架的本地 coding agent。
- `tmp/interactive-report.json` 中对应任务的 `accepted` 应为 `true`。

## 测试 2：连续上下文

输入：

```text
继续上一个问题，说明完成度检查主要在哪里实现
```

预期：

- agent 能利用上一条任务摘要理解“上一个问题”。
- final 应提到 `smallagent/completion.py` 和完成度 harness。
- `/history` 能看到前两条任务摘要。

## 测试 3：高风险命令确认

输入：

```text
运行 python --version 检查当前 Python 版本
```

预期：

- 终端应提示 `即将执行高风险工具：run_shell`。
- 输入 `n` 时，应拒绝执行，并让模型选择其他方式继续或说明无法执行。
- 再次输入同一任务并回答 `y` 时，应执行命令并返回结果。
- 如果模型服务返回空内容或网络中断，可以输入 `/retry` 重新执行最近一条任务。

## 测试 4：临时目录写文件

先在 PowerShell 创建临时工作区：

```powershell
mkdir tmp/manual-agent
python -m smallagent --workspace tmp/manual-agent --interactive --history-file tmp/manual-history.json --report-file tmp/manual-report.json
```

输入：

```text
创建 note.txt，内容写 SmallAgent manual test，然后读回文件确认
```

预期：

- agent 应调用 `write_file` 创建文件。
- 修改后应调用 `read_file` 或其他验证工具。
- `tmp/manual-agent/note.txt` 应存在，内容正确。
- report 中应出现 `mutation`、`verification` 和 `changed_files`。

## 测试 4.1：细粒度编辑

继续在临时工作区输入：

```text
创建 notes 目录，在 notes/log.txt 中写入第一行，再追加第二行，然后读回确认
```

预期：

- agent 可使用 `create_directory`、`write_file` 或 `append_text` 完成任务。
- report 中应出现目录或文件变化。
- 如果要求插入或替换某几行，agent 应优先使用 `insert_text` 或 `replace_lines`，而不是重写整个文件。

## 测试 5：推荐验证命令

在项目根目录交互模式中输入：

```text
发现这个项目推荐的验证命令，并运行它
```

预期：

- agent 应发现 `scripts/check.ps1` 或 Python unittest。
- 运行前应弹出高风险确认。
- 输入 `y` 后应运行验证命令。
- final 应说明测试数量和结果。

## 判断可用的标准

- 能先查看上下文再回答，不凭空总结。
- 能在交互模式连续处理多条任务。
- 能把上一条任务摘要注入下一条任务。
- 能在高风险命令前询问用户。
- 能写入文件并在修改后验证。
- 能保存 `history` 和 `report`，并在下次启动时恢复摘要。
- 遇到模型空响应、网络中断等临时失败时，可以用 `/retry` 快速重试。
- 自动测试 `scripts/check.ps1` 通过。
