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
10. 如果动作是最终回答，或达到步数上限，循环停止。

## 可扩展位置

- 新增工具：在 `ToolRegistry.__post_init__` 中注册函数。
- 替换模型：实现 `ChatClient` 协议即可。
- 调整行为：修改 `smallagent.prompts.SYSTEM_PROMPT`。
- 加强安全策略：扩展 `ToolRegistry._check_command`。
- 扩展记忆：替换 `ShortTermMemory` 为文件存储、数据库或向量检索。
- 扩展规划：替换 `Planner`，加入任务分解、检查点和回滚策略。
- 扩展决策：替换 `DecisionPolicy`，加入风险分级、人工确认和工具白名单。

## 边界约束

- 不依赖 LangChain、LlamaIndex、OpenAI Agents SDK 等 agent 框架。
- 不使用服务端托管的代码执行或文件工具。
- 文件路径必须解析在工作区内，越界访问会被拒绝。
- API key 只从环境变量或未入库的 `.env` 中读取。
