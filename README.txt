SmallAgent

Git 仓库地址：https://github.com/smallbailangui/SmallAgent

运行方式：
1. 安装 Python 3.10+。
2. 参考 .env.example 设置环境变量 OPENAI_API_KEY；程序会自动读取当前目录的 .env，可选设置 SMALLAGENT_MODEL、OPENAI_BASE_URL。兼容服务也支持 BASE_URL 作为 OPENAI_BASE_URL 的别名。
3. 执行：python -m smallagent "你的编程任务"
4. 如需保存演示记录，加 --history-file tmp/history.json。
5. 如需保存包含验收标准和结构化证据的完成度报告，加 --report-file tmp/report.json。
6. 如需连续输入多个任务，执行：python -m smallagent --interactive。
7. 交互模式也可以同时加 --history-file tmp/interactive-history.json 和 --report-file tmp/interactive-report.json；文件会保存为 JSON 数组，每条任务追加一条记录。

交互式终端：
进入 --interactive 后，可以连续输入普通任务；每条任务都会走完整的模型、工具、验收闭环。
终端层会保留最近任务摘要，并在下一条任务开始时注入给 agent，帮助它理解“继续”“它”等上下文指代。
内置命令包括：
- /help：查看可用命令。
- /status：查看当前工作区、模型、接口地址、最大执行轮数、记录文件和已完成任务数。
- /history：查看本次终端会话内的任务摘要。
- /clear：清空本次终端会话摘要。
- /retry：重新执行最近一条任务，适合模型空响应或网络中断后快速重试。
- /exit 或 /quit：退出交互模式。
当模型准备执行 run_shell 或 run_recommended_verification 这类高风险工具时，交互式终端会提示是否允许执行；只有输入 y 或 yes 才会继续。

测试：
python -m unittest discover -s tests -v
或执行：powershell -ExecutionPolicy Bypass -File scripts/check.ps1
真实使用前的手动测试清单见 docs/manual-test.md。

特色功能：
SmallAgent 是一个不依赖 LangChain、LlamaIndex、OpenAI Agents SDK 等 agent 框架的简化编程智能体。它用 OpenAI 兼容 Chat Completions 与模型交互，但本地核心逻辑自行实现：终端层支持连续输入多个任务和高风险工具人工确认，感知层整理任务和工具观察，规划层维护可解释步骤，记忆层保存短期运行事实，决策层校验模型动作是否可执行，完成度检查层根据任务验收标准、结构化工具证据、工作区快照变化和推荐验证命令复核 final 是否可以停止；当 final 阶段只缺推荐验证时，agent 会自动运行一次推荐命令并重新评估。当前工具包括工作目录、文件列表、读文件、文件元信息、文本搜索、创建目录、写文件、追加文本、按行插入、文本替换、按行替换、执行命令、Git 状态、Git diff、发现推荐验证和运行推荐验证。代码结构刻意拆成 terminal、agent、perception、planning、memory、decision、completion、tools、verification、model、cli、prompts，便于后续扩展长期记忆、风险分级、自检 harness、更多工具或替换模型提供方。

提交说明：
已推送历史不改写；中文提交说明见 docs/git-history.md，后续提交信息使用中文。
