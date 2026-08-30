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
SmallAgent 是一个不依赖 LangChain、LlamaIndex、OpenAI Agents SDK 等 agent 框架的简化编程智能体。它用 OpenAI 兼容 Chat Completions 与模型交互，但本地核心逻辑自行实现：对话历史管理、JSON 动作解析、工具注册与执行、工作区路径保护、危险命令拦截、循环终止和错误反馈。当前工具包括列文件、读文件、写文件、文本替换、执行命令和查看工作目录。代码结构刻意拆成 agent、tools、model、cli、prompts，便于后续扩展更多工具、替换模型提供方或加强安全策略。

提交说明：
已推送历史不改写；中文提交说明见 docs/git-history.md，后续提交信息使用中文。
