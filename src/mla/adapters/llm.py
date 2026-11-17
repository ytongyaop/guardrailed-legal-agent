"""云端模型适配：当前只接 DeepSeek（靠 base_url 可切换兼容 OpenAI 协议的国产 API）。

prompt 全部写在这里；内核不得出现 prompt 字符串。

两条互不干扰的出口（规格 002 第 6 章、第 10.9 节）：
- 生成路径（`generate()` / `chat()`）只读 `cfg.timeout_s`，错误一律包成 `LLMError`，
  语义与改造前完全一致；
- 校验路径（`probe_models()`）只发 `GET /models`、用 `cfg.auth_check_timeout_s`，
  返回结构化的 `ProbeResult`，由设置页 / CLI 映射成三条固定文案。`health()` 保留
  `bool` 返回，内部复用同一请求路径。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from ..config import LLMConfig

# 鉴权失败的状态码（规格 002 第 10.9 节第一行）
AUTH_FAILURE_CODES = frozenset({401, 403})

# 网络类异常：只按类型判定，不看异常文本（禁止字符串匹配）
NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, socket.gaierror, ConnectionError)


class LLMError(RuntimeError):
    """模型调用失败（网络 / 鉴权 / 解析 / 内容为空）。"""


@dataclass(frozen=True)
class ProbeResult:
    """一次 `GET /models` 校验的结构化结果（不含 Key，也不含 Key 长度）。"""

    kind: str  # "ok" | "auth" | "network" | "server"
    status_code: int | None = None
    message: str = ""  # 不含 Key 的诊断，例如 "HTTP 401"


CLASSIFY_SYSTEM = """你是劳动用工领域的分类器。只输出 JSON：{"label": "<给定标签之一>"}。
如果无法判断，输出 {"label": "不确定"}。不要解释。"""


class DeepSeekLLM:
    """实现 ports.generation.Generator（OpenAI 兼容的 /chat/completions）。

    `cfg` 必填：无参时自行读 `load_config()` 的旁路已删除（规格 002 第 6 章），
    调用方必须先经 `services.credentials.resolve_llm_config()` 解析出配置。
    """

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
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

    def _probe_get(self, path: str, key: str, timeout_s: float) -> int:
        """发一次空 payload 的 GET，返回 HTTP 状态码（只服务 `GET /models` 校验）。

        与 `_request` 分开：Key 与超时都由调用方传入，因此既能校验**尚未落盘**的新 Key，
        也绝不会读到推理超时；请求体为空，不构造 messages，不产生 token 费用。
        """
        req = urllib.request.Request(
            self._endpoint(path),
            data=None,
            method="GET",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            resp.read()
            status = getattr(resp, "status", None)
            return int(status) if status is not None else 200

    def probe_models(self, key: str, *, timeout_s: float | None = None) -> ProbeResult:
        """只用 `GET {base_url}/models` 校验一把 Key，返回结构化分类。

        分类判据只有异常类型与 `HTTPError.code`（规格 002 第 10.9 节第 5 条）：
        401 / 403 → `auth`；其他 HTTP 状态码（含 429、5xx）→ `server`；
        网络类异常与超时 → `network`；请求拿到 2xx → `ok`。
        `timeout_s` 不传时用 `cfg.auth_check_timeout_s`，**不用** `cfg.timeout_s`。
        """
        token = (key or "").strip()
        if not token:
            # 没有 Key 就不发请求：归入「Key 不正确」一类，诊断里不带任何 Key 信息
            return ProbeResult(kind="auth", message="未提供 API Key")
        timeout = self.cfg.auth_check_timeout_s if timeout_s is None else timeout_s
        try:
            code = self._probe_get("/models", token, timeout)
        except urllib.error.HTTPError as exc:
            kind = "auth" if exc.code in AUTH_FAILURE_CODES else "server"
            return ProbeResult(kind=kind, status_code=int(exc.code), message=f"HTTP {exc.code}")
        except NETWORK_ERRORS as exc:
            return ProbeResult(kind="network", message=type(exc).__name__)
        except OSError as exc:  # 连接被重置等底层 I/O 失败同样算网络问题
            return ProbeResult(kind="network", message=type(exc).__name__)
        return ProbeResult(kind="ok", status_code=code, message=f"HTTP {code}")

    def health(self) -> bool:
        """轻量连通性检查（不消耗生成额度）。返回 bool、签名不变，内部复用探针。"""
        if not self.available:
            self.last_error = "未配置 API Key"
            return False
        result = self.probe_models(self.cfg.api_key)
        if result.kind == "ok":
            return True
        self.last_error = result.message or f"探测失败（{result.kind}）"
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
