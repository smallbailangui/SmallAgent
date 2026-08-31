# SmallAgent 设计说明

SmallAgent 故意保持小而清晰，方便在面试中解释每一个关键环节。

## 运行流程

1. `smallagent.cli` 解析任务、工作目录和最大循环轮数。
2. `smallagent.config` 从 `.env` 读取本地配置，系统环境变量优先。
3. `smallagent.perception` 形成感知状态，包括任务、工作区、轮数和最近工具结果。
4. `smallagent.planning` 维护当前计划，记录任务推进过程。
5. `smallagent.memory` 保存短期记忆，让后续轮次能看到关键事实。
6. `smallagent.model` 调用 OpenAI 兼容的聊天补全接口。
7. `smallagent.agent` 解析模型返回的 JSON 动作。
8. `smallagent.decision` 校验动作是否允许执行。
9. 如果动作是工具调用，`smallagent.tools` 在本地执行并把观察结果交还给模型。
10. 如果动作是最终回答，`smallagent.completion` 先根据任务验收标准和工具轨迹执行完成度检查。
11. 完成度检查通过，或达到步数上限，循环停止；未通过时把 `SELF_CHECK` 反馈给模型继续补证据。

## 可扩展位置

- 新增工具：在 `ToolRegistry.__post_init__` 中注册函数。
- 替换模型：实现 `ChatClient` 协议即可。
- 调整行为：修改 `smallagent.prompts.SYSTEM_PROMPT`。
- 加强安全策略：扩展 `ToolRegistry._check_command`。
- 扩展记忆：替换 `ShortTermMemory` 为文件存储、数据库或向量检索。
- 扩展规划：替换 `Planner`，加入任务分解、检查点和回滚策略。
- 扩展决策：`DecisionPolicy` 已输出基础风险等级，后续可加入人工确认和工具白名单。
- 扩展完成度检查：`CompletionHarness` 已在 final 前检查空回答、未处理失败、修改后验证证据和任务验收标准；后续可接入测试发现、静态分析和更细粒度的完成判据。

## 完成度检查

当前完成度检查是一个很小的 harness 雏形，目标不是替代模型判断，而是在停止前要求 agent 给出可检查的证据。每次任务开始时，`CompletionHarness` 会从任务文本生成保守的验收标准，拍摄工作区文件快照，发现项目推荐验证命令，并把标准和命令放进后续状态提示。每次工具调用后，harness 会把原始工具结果提炼为结构化 `Evidence`，例如 `context`、`mutation`、`verification`、`shell`、`recommended_verification` 或 `failure`。

- final 消息不能为空，必须说明完成内容和验证方式。
- 如果最近一次工具失败，不能直接结束，需要先修复或产生新的成功证据。
- 如果成功修改了文件，必须在之后成功执行 `read_file`、`list_files` 或 `run_shell` 之一，证明 agent 至少复查过修改结果。
- 如果任务文本暗示需要改动、测试或检查，完成度检查会要求对应的成功工具证据，例如文件修改工具或 `run_shell`。
- 如果 harness 发现了项目推荐验证命令，例如 `scripts/check.ps1`、Python unittest、`npm test`、`cargo test` 或 `go test ./...`，相关任务完成前需要成功运行推荐命令之一。
- 如果 final 声称测试或检查通过，但没有成功的 shell 验证证据，完成度检查会拒绝结束。
- 如果任务要求改动且 harness 有工作区快照，报告会列出 added、modified、deleted 文件；验收标准也会要求快照中能看到真实文件变化。

这个模块故意只依赖任务文本和结构化证据，方便后续演进为更强的自检系统，例如让模型生成结构化验收清单、自动选择测试命令、对比文件变更和 final 声明是否一致。

`AgentResult.completion_check` 会保留最后一次完成度检查结果，包括验收标准状态、结构化验收结果、最近证据摘要、推荐验证命令和工作区变化。调用方无需解析对话历史即可判断任务是通过验收结束，还是因为步数上限等原因停止。

命令行入口支持 `--report-file`，会把最终回答、执行轮数和完成度检查写成 JSON。报告中的 `completion_check.accepted` 可作为总体通过标记，`criteria_results` 包含每条验收标准的 `key`、`description`、`met` 和 `evidence_indices`，`evidence` 保存带标签和调用参数摘要的工具证据，`changed_files` 保存工作区快照 diff，`verification_commands` 保存 harness 发现的推荐验证命令。这个出口可以被外部考核脚本读取，用来判断 agent 是否给出了可验收的完成证据。

## 边界约束

- 不依赖 LangChain、LlamaIndex、OpenAI Agents SDK 等 agent 框架。
- 不使用服务端托管的代码执行或文件工具。
- 文件路径必须解析在工作区内，越界访问会被拒绝。
- API key 只从环境变量或未入库的 `.env` 中读取。
