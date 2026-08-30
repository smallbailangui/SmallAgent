# Demo Script

This short scenario is suitable for the required two-minute video.

## Setup

Use a clean scratch folder inside the project:

```powershell
New-Item -ItemType Directory -Force tmp/demo-workspace
```

Set `OPENAI_API_KEY` in the shell before recording. Keep the key off screen.

## Task To Run

```powershell
python -m smallagent --workspace tmp/demo-workspace --history-file tmp/demo-history.json "Create a Python function in calculator.py that adds two numbers, then create a unittest file and run the tests."
```

## What To Show

1. The agent reads or creates files through its local tools.
2. The agent runs tests with `run_shell`.
3. The final answer reports what changed.
4. `tmp/demo-history.json` records model actions and tool observations.

## Talking Points

- No agent framework is used.
- The model only emits JSON actions.
- Tool execution, path checks, command policy, history, and loop stopping are implemented locally.
