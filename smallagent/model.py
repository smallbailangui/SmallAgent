"""基于标准库实现的 OpenAI 兼容聊天补全客户端。

这个模块是 agent 和大模型 API 之间的边界层。它不理解工具、不修改文件，
只负责把 messages 发给 /chat/completions，再取回模型返回的 content 文本。
"""

from __future__ import annotations

import json
import os
import http.client
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class OpenAICompatibleClient:
    """一个最小模型客户端。

    字段基本对应一次 HTTP 请求需要的配置：密钥、模型名、服务地址、采样温度和超时。
    """

    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    temperature: float = 0.2
    timeout: int = 90
    max_retries: int = 1

    @classmethod
    def from_env(cls) -> "OpenAICompatibleClient":
        """从环境变量创建客户端。

        这里故意使用 OPENAI_* 命名，即使连接 DeepSeek 等兼容服务，也要通过
        OPENAI_BASE_URL 指向对应地址，方便复用同一套 Chat Completions 协议。
        """
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required")
        return cls(
            api_key=api_key,
            model=os.getenv("SMALLAGENT_MODEL", "gpt-4o-mini"),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            temperature=float(os.getenv("SMALLAGENT_TEMPERATURE", "0.2")),
            max_retries=int(os.getenv("SMALLAGENT_MODEL_RETRIES", "1")),
        )

    def complete(self, messages: list[dict[str, str]]) -> str:
        """发送一轮聊天补全请求，并返回模型消息 content。

        agent.py 会把系统提示词、状态、用户任务和工具观察都放进 messages；
        这个函数只负责序列化、发送、解析响应，不参与动作决策。
        """
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        last_retryable_error: RuntimeError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                payload = self._post_chat_completions(body)
                # Chat Completions 的标准返回位置：choices[0].message.content。
                content = payload["choices"][0]["message"]["content"]
            except http.client.IncompleteRead as exc:
                # 兼容服务偶尔会在 chunked 响应中途断开；这类网络抖动允许重试。
                last_retryable_error = RuntimeError(
                    "model API response ended before it was fully read; please retry the task"
                )
                if attempt < self.max_retries:
                    continue
                raise last_retryable_error from exc
            except urllib.error.HTTPError as exc:
                # 把服务端返回的错误正文带出来，用户能直接看到 401/模型名错误等原因。
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"model API request failed: HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(f"model API request failed: {exc.reason}") from exc
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(f"unexpected model API response: {payload!r}") from exc

            if isinstance(content, str) and content.strip():
                return content

            # 有些兼容服务偶发返回空 message.content；重试一次通常比直接中断体验更好。
            last_retryable_error = RuntimeError("model returned empty content; please retry the task")
            if attempt < self.max_retries:
                continue
            raise last_retryable_error

        # 循环理论上一定会 return 或 raise；保留兜底让类型检查和阅读都更明确。
        raise last_retryable_error or RuntimeError("model request failed")

    def _post_chat_completions(self, body: dict[str, Any]) -> dict[str, Any]:
        """发送 HTTP 请求并解析 JSON 响应。"""
        # OpenAI 兼容接口约定：POST {base_url}/chat/completions。
        request = urllib.request.Request(
            url=f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError(f"unexpected model API response: {payload!r}")
        return payload
