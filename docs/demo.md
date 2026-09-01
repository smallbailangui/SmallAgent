# 演示脚本

下面这个场景适合录制题目要求的 2 分钟以内视频。

## 准备

在项目中创建一个干净的临时工作目录：

```powershell
New-Item -ItemType Directory -Force tmp/demo-workspace
```

录屏前在 `.env` 或命令行环境变量中设置 `OPENAI_API_KEY`，不要让真实 key 出现在画面里。

## 运行任务

```powershell
python -m smallagent --workspace tmp/demo-workspace --history-file tmp/demo-history.json "创建 calculator.py，里面写一个 add(a, b) 函数；再创建 unittest 测试文件并运行测试。"
```

## 展示重点

1. 智能体通过本地工具读取或创建文件。
2. 智能体用 `run_shell` 执行测试。
3. 终端会默认用分隔块实时打印 `Agent 状态汇总（Perception / Planning / Memory）`、`模型行动提案与本地决策（Action Proposal / Decision）`、`工具执行观察（Tool Observation）`、`人工安全确认（Human Approval）` 和 `Final 验收决策（Completion Check）`，重点展示感知、规划、记忆、决策和验收，不展开工具参数和风险等级。
4. 最终回答会说明修改内容和验证结果。
5. `tmp/demo-history.json` 会保存模型动作和工具观察，便于讲解运行过程。

## 讲解要点

- 项目没有使用 agent 框架。
- 模型只输出 JSON 动作。
- 工具执行、路径检查、命令策略、历史记录和循环停止都在本地代码中实现。
