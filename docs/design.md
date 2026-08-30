# SmallAgent 设计说明

SmallAgent 故意保持小而清晰，方便在面试中解释每一个关键环节。

## 运行流程

1. `smallagent.cli` 解析任务、工作目录和最大循环轮数。
2. `smallagent.config` 从 `.env` 读取本地配置，系统环境变量优先。
3. `smallagent.model` 调用 OpenAI 兼容的聊天补全接口。
4. `smallagent.agent` 解析模型返回的 JSON 动作。
5. 如果动作是工具调用，`smallagent.tools` 在本地执行并把观察结果交还给模型。
6. 如果动作是最终回答，或达到步数上限，循环停止。

## 可扩展位置

- 新增工具：在 `ToolRegistry.__post_init__` 中注册函数。
- 替换模型：实现 `ChatClient` 协议即可。
- 调整行为：修改 `smallagent.prompts.SYSTEM_PROMPT`。
- 加强安全策略：扩展 `ToolRegistry._check_command`。

## 边界约束

- 不依赖 LangChain、LlamaIndex、OpenAI Agents SDK 等 agent 框架。
- 不使用服务端托管的代码执行或文件工具。
- 文件路径必须解析在工作区内，越界访问会被拒绝。
- API key 只从环境变量或未入库的 `.env` 中读取。
