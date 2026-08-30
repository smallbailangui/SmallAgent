# SmallAgent Design Notes

SmallAgent is intentionally small so every moving part can be explained in an
interview.

## Runtime Flow

1. `smallagent.cli` parses the task, workspace, and iteration limit.
2. `smallagent.model` sends chat messages to an OpenAI-compatible API.
3. `smallagent.agent` parses the model's JSON response.
4. If the response is a tool call, `smallagent.tools` executes it locally and
   returns an observation to the model.
5. If the response is final or the step limit is reached, the loop stops.

## Extension Points

- Add tools by registering a new function in `ToolRegistry.__post_init__`.
- Swap model providers by implementing the `ChatClient` protocol.
- Tune behavior by editing `smallagent.prompts.SYSTEM_PROMPT`.
- Add stronger command policy checks in `ToolRegistry._check_command`.

## Boundaries

- The project does not depend on agent frameworks or hosted code execution.
- Files are resolved against the workspace and blocked if they escape it.
- API keys are read from environment variables only.
