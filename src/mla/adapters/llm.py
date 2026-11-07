"""云端模型适配：当前只接 DeepSeek（靠 base_url 可切换兼容 OpenAI 协议的国产 API）。

prompt 全部写在这里；内核不得出现 prompt 字符串。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from ..config import LLMConfig, load_config


class LLMError(RuntimeError):
    """模型调用失败（网络 / 鉴权 / 解析 / 内容为空）。"""


CLASSIFY_SYSTEM = """你是劳动用工领域的分类器。只输出 JSON：{"label": "<给定标签之一>"}。
如果无法判断，输出 {"label": "不确定"}。不要解释。"""


class DeepSeekLLM:
    """实现 ports.generation.Generator（OpenAI 兼容的 /chat/completions）。"""

    def __init__(self, cfg: LLMConfig | None = None) -> None:
        self.cfg = cfg or load_config().llm
        self.last_error = ""
        self.usage_in = 0
        self.usage_out = 0

    @property
    def available(self) -> bool:
        return bool(self.cfg.api_key)

    def _endpoint(self, path: str) -> str:
        base = (self.cfg.base_url or "https://api.deepseek.com").rstrip("/")
        return f"{base}{path}"

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._endpoint(path),
            data=data,
            method="GET" if payload is None else "POST",
            headers={
                "Authorization": f"Bearer {self.cfg.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:200]
            raise LLMError(f"HTTP {exc.code}：{detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMError(f"调用失败：{exc}") from exc

    def _record_usage(self, data: dict[str, Any]) -> None:
        usage = data.get("usage") or {}
        self.usage_in += int(usage.get("prompt_tokens") or 0)
        self.usage_out += int(usage.get("completion_tokens") or 0)

    def health(self) -> bool:
        """轻量连通性检查（不消耗生成额度）。"""
        if not self.available:
            self.last_error = "未配置 API Key"
            return False
        try:
            self._request("/models")
            return True
        except LLMError as exc:
            self.last_error = str(exc)
            return False

    def generate(self, system: str, user: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.cfg.model or "deepseek-chat",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.cfg.temperature,
            "stream": False,
        }
        if schema is not None:
            payload["response_format"] = {"type": "json_object"}

        last: Exception | None = None
        for _attempt in range(2):
            try:
                data = self._request("/chat/completions", payload)
                self._record_usage(data)
                content = data["choices"][0]["message"]["content"]
                return self._parse(content)
            except (LLMError, KeyError, IndexError, json.JSONDecodeError) as exc:
                last = exc
                time.sleep(0.5)
        self.last_error = str(last)
        raise LLMError(f"模型返回不可用：{last}")

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """一轮对话（可带工具调用）；返回 assistant message 原始结构。"""
        payload: dict[str, Any] = {
            "model": self.cfg.model or "deepseek-chat",
            "messages": messages,
            "temperature": self.cfg.temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_format is not None:
            payload["response_format"] = response_format
        last: Exception | None = None
        for _attempt in range(2):
            try:
                data = self._request("/chat/completions", payload)
                self._record_usage(data)
                return dict(data["choices"][0]["message"])
            except (LLMError, KeyError, IndexError) as exc:
                last = exc
                time.sleep(0.5)
        self.last_error = str(last)
        raise LLMError(f"工具调用失败：{last}")

    @staticmethod
    def _parse(content: str) -> dict[str, Any]:
        text = (content or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text.split("\n", 1)[1] if "\n" in text else text
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise
            value = json.loads(text[start : end + 1])
        if not isinstance(value, dict):
            raise LLMError("模型返回不是 JSON 对象")
        return value

    def classify(self, text: str, labels: list[str]) -> str:
        if not self.available:
            return "不确定"
        out = self.generate(CLASSIFY_SYSTEM, f"标签：{labels}\n文本：{text}", {"type": "object"})
        label = str(out.get("label", ""))
        return label if label in labels else "不确定"
