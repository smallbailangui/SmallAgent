# 演示脚本

下面这个场景适合录制 2 分钟以内视频。案例是让 SmallAgent 在一个空的 `tmp` 工作目录中，用交互式模式完成 C++ 命令行停车收费计算器。

## 准备

创建一个干净的临时工作目录：

```powershell
New-Item -ItemType Directory -Force tmp/parking-demo
```

录屏前在 `.env` 或命令行环境变量中设置 `OPENAI_API_KEY`，不要让真实 key 出现在画面里。录屏环境还需要可用的 C++ 编译器，例如 `g++`。

## 启动交互式 agent

```powershell
python -m smallagent --workspace tmp/parking-demo --interactive --history-file tmp/parking-history.json --report-file tmp/parking-report.json
```

进入 `SmallAgent>` 后，粘贴下面的任务：

```text
请在当前空目录中实现一个 C++ 命令行停车收费计算器。

要求：
1. 创建 parking.cpp。
2. 程序从标准输入读取停车时长，单位为分钟，输入一个整数。
3. 收费规则：
- 停车时间 <= 30 分钟：免费
- 超过 30 分钟后，每不足 1 小时按 1 小时计算
- 每小时收费 5 元
- 单次停车最高收费 50 元
4. 示例：
- 输入 20，输出 0
- 输入 31，输出 5
- 输入 90，输出 5
- 输入 91，输出 10
- 输入 700，输出 50
5. 输入负数时输出 Invalid input

请先查看工作区，再创建代码文件，然后用 g++ 编译并运行示例输入验证，最后总结修改内容和验证结果。
```

如果 agent 请求执行 `run_shell`，输入 `y` 允许它编译和运行测试。

## 建议保留的录屏片段

1. `Agent 状态汇总（Perception / Planning / Memory）`：展示 agent 每轮会整理感知、规划、记忆和验收条件。
2. `模型行动提案与本地决策（Action Proposal / Decision）`：展示模型提出动作，本地决策层再决定是否执行。
3. `工具执行观察（Tool Observation）`：展示本地工具创建 `parking.cpp`、读取文件、编译运行。
4. `人工安全确认（Human Approval）`：展示运行 shell 命令前需要用户确认。
5. `Final 验收决策（Completion Check）`：展示最终回答不是直接相信模型，而是经过本地验收。
6. `最终总结`：展示 agent 汇总创建了什么、运行了哪些验证、结果如何。

## 可手动复核的命令

如果需要在视频后半段手动展示结果，可以在另一个终端运行：

```powershell
g++ -std=c++17 parking.cpp -o parking
"20", "31", "90", "91", "700", "-1" | ForEach-Object { $_ | .\parking }
```

预期输出：

```text
0
5
5
10
50
Invalid input
```

## 视频介绍词

可以这样讲：

```text
这个演示展示的是我自己实现的一个小型 coding agent。它没有使用 LangChain、LlamaIndex 或 OpenAI Agents SDK，而是自己实现了 agent 主循环。

我现在用交互式模式把工作区限制在 tmp/parking-demo 这个空目录里，让 agent 完成一个 C++ 停车收费计算器。终端里的分隔块会展示它每一轮的感知、规划、短期记忆、模型行动提案、本地决策、工具观察和最终验收。

可以看到，agent 不是直接输出一段代码就结束，而是先观察工作区，再创建 parking.cpp，通过本地工具编译运行示例输入，最后由完成度检查确认有足够证据后才输出总结。
```

## 讲解要点

- 项目没有使用 agent 框架。
- 模型只输出 JSON 动作，真正的文件读写和命令执行由本地工具层完成。
- 终端展示的是公开执行轨迹，不是隐藏推理链。
- `history` 和 `report` 会保存完整运行记录，便于复盘和评分。
