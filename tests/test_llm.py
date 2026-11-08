"""DeepSeek 适配的错误路径：HTTPError / 超时 / 非法 JSON 一律包装成 LLMError，并带一次重试。

不联网：`urlopen` 换成假实现，`time.sleep` 打桩（重试间隔 0.5s 不真等）。
"""

from __future__ import annotations

import io
import urllib.error
from collections.abc import Callable

import pytest

from mla.adapters import llm as llm_mod
from mla.adapters.llm import DeepSeekLLM, LLMError
from mla.config import LLMConfig

CFG = LLMConfig(api_key="test-key", timeout_s=1)


class _FakeResponse:
    """够用的假响应：支持 `with urlopen(...) as resp: resp.read()`。"""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _patch(monkeypatch: pytest.MonkeyPatch, behaviour: Callable[..., object]) -> dict[str, list]:
    """替换 urlopen 与 sleep，返回可断言的调用记录。"""
    record: dict[str, list] = {"attempts": [], "sleeps": []}

    def fake_urlopen(req: object, timeout: float | None = None) -> object:
        record["attempts"].append(req)  # type: ignore[arg-type]
        return behaviour()

    def fake_sleep(seconds: float) -> None:
        record["sleeps"].append(seconds)

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm_mod.time, "sleep", fake_sleep)
    return record


def _raise_http_500() -> object:
    raise urllib.error.HTTPError(
        "https://api.deepseek.com/chat/completions",
        500,
        "Internal Server Error",
        {},  # type: ignore[arg-type]
        io.BytesIO(b'{"error": "boom"}'),
    )


def _raise_timeout() -> object:
    raise TimeoutError("timed out")


def _return_bad_json() -> object:
    return _FakeResponse(b"<html>not json</html>")


def test_generate_wraps_http_error_with_detail_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 错误被包成 LLMError，带状态码与响应片段；重试一次后仍失败才抛出。"""
    record = _patch(monkeypatch, _raise_http_500)
    llm = DeepSeekLLM(CFG)

    with pytest.raises(LLMError) as excinfo:
        llm.generate("sys", "user")

    message = str(excinfo.value)
    assert "模型返回不可用" in message
    assert "HTTP 500" in message and "boom" in message  # 细节没丢
    assert len(record["attempts"]) == 2  # 两次尝试 = 一次重试
    assert record["sleeps"] == [0.5, 0.5]
    assert "HTTP 500" in llm.last_error


def test_generate_wraps_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时同样统一包装，不再把底层异常漏给调用方。"""
    record = _patch(monkeypatch, _raise_timeout)

    with pytest.raises(LLMError) as excinfo:
        DeepSeekLLM(CFG).generate("sys", "user")

    message = str(excinfo.value)
    assert "模型返回不可用" in message and "调用失败" in message and "timed out" in message
    assert len(record["attempts"]) == 2


def test_generate_wraps_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """响应体不是 JSON 时由 _request 里的 json.loads 抛错，最终也归到 LLMError。"""
    record = _patch(monkeypatch, _return_bad_json)

    with pytest.raises(LLMError) as excinfo:
        DeepSeekLLM(CFG).generate("sys", "user")

    assert "调用失败" in str(excinfo.value)
    assert len(record["attempts"]) == 2  # 重试后才失败
    assert record["sleeps"] == [0.5, 0.5]


def test_generate_parses_choice_content_and_records_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """正常路径的对照：假响应能被解析，用量被记账，且不发生重试等待。"""
    body = (
        b'{"choices": [{"message": {"content": "{\\"label\\": \\"\xe5\x90\x88\xe8\xa7\x84\\"}"}}],'
        b' "usage": {"prompt_tokens": 11, "completion_tokens": 7}}'
    )
    record = _patch(monkeypatch, lambda: _FakeResponse(body))
    llm = DeepSeekLLM(CFG)

    assert llm.generate("sys", "user") == {"label": "合规"}
    assert (llm.usage_in, llm.usage_out) == (11, 7)
    assert len(record["attempts"]) == 1 and record["sleeps"] == []


def test_chat_wraps_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具调用那条路径的重试与包装口径一致，只是错因文案不同。"""
    record = _patch(monkeypatch, _raise_http_500)

    with pytest.raises(LLMError) as excinfo:
        DeepSeekLLM(CFG).chat([{"role": "user", "content": "hi"}])

    assert "工具调用失败" in str(excinfo.value) and "HTTP 500" in str(excinfo.value)
    assert len(record["attempts"]) == 2


def test_health_without_key_skips_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 Key 时健康检查直接失败，不发请求。"""
    record = _patch(monkeypatch, _raise_http_500)
    llm = DeepSeekLLM(LLMConfig(api_key=""))

    assert llm.health() is False
    assert llm.last_error == "未配置 API Key"
    assert record["attempts"] == []


def test_health_reports_error_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """有 Key 但接口报错：health 返回 False 并留下错因，不抛异常。"""
    _patch(monkeypatch, _raise_http_500)
    llm = DeepSeekLLM(CFG)

    assert llm.health() is False
    assert "HTTP 500" in llm.last_error


def test_parse_rejects_non_object_json() -> None:
    """模型返回数组等非对象结构时按"不是 JSON 对象"报错。"""
    with pytest.raises(LLMError, match="不是 JSON 对象"):
        DeepSeekLLM._parse("[]")
