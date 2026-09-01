# SmallAgent 设计说明

SmallAgent 故意保持小而清晰，方便在面试中解释每一个关键环节。

## 运行流程

1. `smallagent.cli` 解析任务、工作目录和最大循环轮数。
2. `smallagent.config` 从 `.env` 读取本地配置，系统环境变量优先。
3. 如果使用 `--interactive`，`smallagent.terminal` 进入持续输入循环，普通输入会被当作一条新任务执行，斜杠命令由终端层直接处理。
4. `smallagent.perception` 形成感知状态，包括任务、工作区、轮数和最近工具结果。
5. `smallagent.planning` 维护当前计划，记录任务推进过程。
6. `smallagent.memory` 保存短期记忆，让后续轮次能看到关键事实。
7. `smallagent.model` 调用 OpenAI 兼容的聊天补全接口。
8. `smallagent.agent` 解析模型返回的 JSON 动作。
9. `smallagent.decision` 校验动作是否允许执行。
10. 如果动作是工具调用，`smallagent.tools` 在本地执行并把观察结果交还给模型。
11. 如果动作是最终回答，`smallagent.completion` 先根据任务验收标准和工具轨迹执行完成度检查。
12. 完成度检查通过，或达到步数上限，循环停止；未通过时把 `SELF_CHECK` 反馈给模型继续补证据。

## 交互式终端

`smallagent.terminal` 是单次任务 agent 外面的一层 REPL 外壳。它的职责是管理终端输入输出和本次会话摘要，不直接做模型推理，也不直接执行文件工具。

当前交互模式支持：

- 普通文本：作为一条新任务交给 `CodingAgent.run()`。
- `/help`：显示内置命令。
- `/status`：显示工作区、最大轮数和本次会话已完成任务数。
- `/history`：显示本次终端会话内的任务摘要和 final 结果预览。
- `/clear`：清空本次终端会话摘要。
- `/exit` 或 `/quit`：退出交互模式。

这个设计先实现“持续输入多个任务”，但每条任务内部仍然是独立的 `CodingAgent` 运行。这样可以先获得终端 agent 的使用形态，同时避免过早把单任务 memory、completion harness 和会话级长期记忆混在一起。后续如果要让 agent 真正跨任务继承上下文，可以在 `TerminalSession` 中保留更丰富的 session memory，并在创建 `CodingAgent` 时注入到 prompt 或 memory 模块。

## 可扩展位置

- 新增工具：在 `ToolRegistry.__post_init__` 中注册函数。
- 替换模型：实现 `ChatClient` 协议即可。
- 调整行为：修改 `smallagent.prompts.SYSTEM_PROMPT`。
- 加强安全策略：扩展 `ToolRegistry._check_command`。
- 扩展记忆：替换 `ShortTermMemory` 为文件存储、数据库或向量检索。
- 扩展规划：替换 `Planner`，加入任务分解、检查点和回滚策略。
- 扩展决策：`DecisionPolicy` 已输出基础风险等级，后续可加入人工确认和工具白名单。
- 扩展完成度检查：`CompletionHarness` 已在 final 前检查空回答、未处理失败、修改后验证证据和任务验收标准；后续可接入测试发现、静态分析和更细粒度的完成判据。
- 扩展交互终端：`TerminalSession` 已支持连续任务和内置命令；后续可加入任务中断、继续执行、人工确认、会话级记忆和历史持久化。

## 完成度检查

当前完成度检查是一个很小的 harness 雏形，目标不是替代模型判断，而是在停止前要求 agent 给出可检查的证据。每次任务开始时，`CompletionHarness` 会从任务文本生成保守的验收标准，拍摄工作区文件快照，发现项目推荐验证命令，并把标准和命令放进后续状态提示。每次工具调用后，harness 会把原始工具结果提炼为结构化 `Evidence`，例如 `context`、`mutation`、`verification`、`shell`、`recommended_verification` 或 `failure`。

- final 消息不能为空，必须说明完成内容和验证方式。
- 如果最近一次工具失败，不能直接结束，需要先修复或产生新的成功证据。
- 如果成功修改了文件，必须在之后成功执行 `read_file`、`list_files` 或 `run_shell` 之一，证明 agent 至少复查过修改结果。
- 如果任务文本暗示需要改动、测试或检查，完成度检查会要求对应的成功工具证据，例如文件修改工具或 `run_shell`。
- 如果 harness 发现了项目推荐验证命令，例如 `scripts/check.ps1`、Python unittest、`npm test`、`cargo test` 或 `go test ./...`，相关任务完成前需要成功运行推荐命令之一。
- 如果 final 阶段唯一缺口是推荐验证命令，`smallagent.agent` 会自动运行一次推荐命令，把结果写入 `AUTO_VERIFY` 和结构化证据，再重新评估完成度。
- 如果 final 声称测试或检查通过，但没有成功的 shell 验证证据，完成度检查会拒绝结束。
- 如果任务要求改动且 harness 有工作区快照，报告会列出 added、modified、deleted 文件；验收标准也会要求快照中能看到真实文件变化。

这个模块故意只依赖任务文本和结构化证据，方便后续演进为更强的自检系统，例如让模型生成结构化验收清单、自动选择测试命令、对比文件变更和 final 声明是否一致。

`AgentResult.completion_check` 会保留最后一次完成度检查结果，包括验收标准状态、结构化验收结果、最近证据摘要、推荐验证命令和工作区变化。调用方无需解析对话历史即可判断任务是通过验收结束，还是因为步数上限等原因停止。

命令行入口支持 `--report-file`，会把最终回答、执行轮数和完成度检查写成 JSON。报告中的 `completion_check.accepted` 可作为总体通过标记，`criteria_results` 包含每条验收标准的 `key`、`description`、`met` 和 `evidence_indices`，`evidence` 保存带标签和调用参数摘要的工具证据，`changed_files` 保存工作区快照 diff，`verification_commands` 保存 harness 发现的推荐验证命令。这个出口可以被外部考核脚本读取，用来判断 agent 是否给出了可验收的完成证据。

## 工具层

工具层围绕 coding agent 的常见工作流拆成四类：

- 上下文工具：`get_cwd`、`list_files`、`read_file`、`file_info`、`search_text`，用于看项目形状、读取文件和查找符号或错误文本。
- 编辑工具：`write_file`、`replace_text`，用于最小化文件修改。
- 验证工具：`run_shell`、`discover_verification`、`run_recommended_verification`，用于执行检查命令和 harness 推荐命令。
- Git 工具：`git_status`、`git_diff`，用于查看工作区变更。

工具结果保留 `ok`、`tool`、`output`、`error` 四个基础字段；支持结构化数据的工具还会返回 `metadata`。例如 `search_text` 会返回匹配数量和命中行，`run_shell` 会返回 command、returncode、stdout、stderr，推荐验证工具会额外标记 `recommended_verification`。`smallagent.agent` 会把 metadata 放回 `OBSERVATION`，模型和完成度检查都能使用同一份结构化证据。

## 边界约束

- 不依赖 LangChain、LlamaIndex、OpenAI Agents SDK 等 agent 框架。
- 不使用服务端托管的代码执行或文件工具。
- 文件路径必须解析在工作区内，越界访问会被拒绝。
- API key 只从环境变量或未入库的 `.env` 中读取。
