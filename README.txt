SmallAgent

Git 仓库地址：待绑定公开远程仓库后填写。

运行方式：
1. 设置环境变量 OPENAI_API_KEY。
2. 可选设置 SMALLAGENT_MODEL、OPENAI_BASE_URL。
3. 执行：python -m smallagent "你的编程任务"

当前状态：
项目将实现一个不依赖 agent 框架的简化 coding agent。核心逻辑包括对话历史管理、本地工具定义与执行、模型输出 JSON 解析、循环停止、错误处理和工作区路径保护。
