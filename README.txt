SmallAgent

Git 仓库地址：https://github.com/smallbailangui/SmallAgent

如何运行：
1. 安装 Python 3.10+。
2. 参考 .env.example 配置 OPENAI_API_KEY，可选配置 SMALLAGENT_MODEL、OPENAI_BASE_URL。
3. 单次任务：python -m smallagent "你的编程任务"
4. 交互式任务：
   python -m smallagent --workspace tmp/parking-demo --interactive --history-file tmp/parking-history.json --report-file tmp/parking-report.json
5. 测试：python -m unittest discover -s tests -v

特色功能：
SmallAgent 是一个不依赖 LangChain、LlamaIndex、OpenAI Agents SDK 等框架的简化编程智能体。模型只输出 JSON 动作，本地代码负责解析、决策和执行。终端默认展示状态汇总、行动提案、工具观察、人工确认和 final 验收；如需关闭，加 --no-trace。

Agent 设计组成：
- CLI/Terminal：解析参数、限制 workspace、支持交互式输入和内置命令。
- Perception：整理任务、工作区、轮数和最近工具观察。
- Planning：维护轻量计划，记录执行进展。
- Memory：保存任务内短期记忆。
- Model Client：调用 OpenAI 兼容 Chat Completions。
- Decision Policy：校验 JSON 动作、工具风险和权限。
- ToolRegistry：执行文件、编辑、shell、Git 和验证工具。
- Completion Harness：在 final 前检查修改、验证、失败工具和证据是否充分。

其它说明：
API key 只从环境变量或未入库的 .env 读取。history/report 保存运行轨迹和验收证据。完整设计图见 docs/design.md，演示脚本见 docs/demo.md。
