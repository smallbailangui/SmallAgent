SmallAgent

Git 仓库地址：https://github.com/smallbailangui/SmallAgent

运行方式：
1. 安装 Python 3.10+。
2. 参考 .env.example 设置环境变量 OPENAI_API_KEY；程序会自动读取当前目录的 .env，可选设置 SMALLAGENT_MODEL、OPENAI_BASE_URL。
3. 执行：python -m smallagent "你的编程任务"
4. 如需保存演示记录，加 --history-file tmp/history.json。
5. 如需保存包含验收标准和结构化证据的完成度报告，加 --report-file tmp/report.json。
6. 如需连续输入多个任务，执行：python -m smallagent --interactive。

交互式终端：
进入 --interactive 后，可以连续输入普通任务；每条任务都会走完整的模型、工具、验收闭环。
内置命令包括：
- /help：查看可用命令。
- /status：查看当前工作区、最大执行轮数和已完成任务数。
- /history：查看本次终端会话内的任务摘要。
- /clear：清空本次终端会话摘要。
- /exit 或 /quit：退出交互模式。

测试：
python -m unittest discover -s tests -v
或执行：powershell -ExecutionPolicy Bypass -File scripts/check.ps1

特色功能：
SmallAgent 是一个不依赖 LangChain、LlamaIndex、OpenAI Agents SDK 等 agent 框架的简化编程智能体。它用 OpenAI 兼容 Chat Completions 与模型交互，但本地核心逻辑自行实现：终端层支持连续输入多个任务，感知层整理任务和工具观察，规划层维护可解释步骤，记忆层保存短期运行事实，决策层校验模型动作是否可执行，完成度检查层根据任务验收标准、结构化工具证据、工作区快照变化和推荐验证命令复核 final 是否可以停止；当 final 阶段只缺推荐验证时，agent 会自动运行一次推荐命令并重新评估。当前工具包括工作目录、文件列表、读文件、文件元信息、文本搜索、写文件、文本替换、执行命令、Git 状态、Git diff、发现推荐验证和运行推荐验证。代码结构刻意拆成 terminal、agent、perception、planning、memory、decision、completion、tools、verification、model、cli、prompts，便于后续扩展长期记忆、风险分级、人工确认、自检 harness、更多工具或替换模型提供方。

提交说明：
已推送历史不改写；中文提交说明见 docs/git-history.md，后续提交信息使用中文。
