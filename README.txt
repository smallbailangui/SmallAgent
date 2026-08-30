SmallAgent

Git 仓库地址：https://github.com/smallbailangui/SmallAgent

运行方式：
1. 安装 Python 3.10+。
2. 参考 .env.example 设置环境变量 OPENAI_API_KEY；程序会自动读取当前目录的 .env，可选设置 SMALLAGENT_MODEL、OPENAI_BASE_URL。
3. 执行：python -m smallagent "你的编程任务"
4. 如需保存演示记录，加 --history-file tmp/history.json。

测试：
python -m unittest discover -s tests -v
或执行：powershell -ExecutionPolicy Bypass -File scripts/check.ps1

特色功能：
SmallAgent 是一个不依赖 LangChain、LlamaIndex、OpenAI Agents SDK 等 agent 框架的简化编程智能体。它用 OpenAI 兼容 Chat Completions 与模型交互，但本地核心逻辑自行实现：感知层整理任务和工具观察，规划层维护可解释步骤，记忆层保存短期运行事实，决策层校验模型动作是否可执行。当前工具包括列文件、读文件、写文件、文本替换、执行命令和查看工作目录。代码结构刻意拆成 agent、perception、planning、memory、decision、tools、model、cli、prompts，便于后续扩展长期记忆、风险分级、人工确认、更多工具或替换模型提供方。

提交说明：
已推送历史不改写；中文提交说明见 docs/git-history.md，后续提交信息使用中文。
