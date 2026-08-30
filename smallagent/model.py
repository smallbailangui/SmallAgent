"""基于标准库实现的 OpenAI 兼容聊天补全客户端。"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class OpenAICompatibleClient:
    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    temperature: float = 0.2
    timeout: int = 90

    @classmethod
    def from_env(cls) -> "OpenAICompatibleClient":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required")
        return cls(
            api_key=api_key,
            model=os.getenv("SMALLAGENT_MODEL", "gpt-4o-mini"),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            temperature=float(os.getenv("SMALLAGENT_TEMPERATURE", "0.2")),
        )

    def complete(self, messages: list[dict[str, str]]) -> str:
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        request = urllib.request.Request(
            url=f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"model API request failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"model API request failed: {exc.reason}") from exc

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"unexpected model API response: {payload!r}") from exc

        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("model returned empty content")
        return content
