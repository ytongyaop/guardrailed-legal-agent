"""DeepSeek 适配的错误路径：HTTPError / 超时 / 非法 JSON 一律包装成 LLMError，并带一次重试。

另有校验探针（`probe_models`）的契约测试：只发一次 `GET /models`、只按异常类型与状态码
分类、超时取 `auth_check_timeout_s`（与推理超时互不串用）。

不联网：`urlopen` 换成假实现，`time.sleep` 打桩（重试间隔 0.5s 不真等）。
"""

from __future__ import annotations

import io
import socket
import urllib.error
from collections.abc import Callable

import pytest

from mla.adapters import llm as llm_mod
from mla.adapters.llm import DeepSeekLLM, LLMError, ProbeResult
from mla.config import LLMConfig

CFG = LLMConfig(api_key="test-key", timeout_s=1)
PROBE_CFG = LLMConfig(api_key="cfg-key", timeout_s=60, auth_check_timeout_s=10)
PROBE_KEY = "test-key"


class _FakeResponse:
    """够用的假响应：支持 `with urlopen(...) as resp: resp.read()`。"""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

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


def _patch_probe(monkeypatch: pytest.MonkeyPatch, behaviour: Callable[..., object]) -> dict[str, list]:
    """替换 urlopen，同时记下每次请求与 timeout（探针契约测试用）。"""
    record: dict[str, list] = {"requests": [], "timeouts": []}

    def fake_urlopen(req: object, timeout: float | None = None) -> object:
        record["requests"].append(req)
        record["timeouts"].append(timeout)
        return behaviour()

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
    return record


def _raise_http(code: int) -> Callable[..., object]:
    """指定状态码的 HTTPError（探针分类测试用）。"""

    def behaviour() -> object:
        raise urllib.error.HTTPError(
            "https://api.deepseek.com/models",
            code,
            "boom",
            {},  # type: ignore[arg-type]
            io.BytesIO(b'{"error": "boom"}'),
        )

    return behaviour


def _raise(error: BaseException) -> Callable[..., object]:
    """抛出指定异常实例（网络类分类测试用）。"""

    def behaviour() -> object:
        raise error

    return behaviour


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


# ---------------------------------------------------------------- 校验探针


def test_probe_models_ok_hits_only_models_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """2xx → ok：只发一次 GET /models，空 payload，不碰生成接口。"""
    record = _patch_probe(monkeypatch, lambda: _FakeResponse(b'{"data": []}', status=200))

    result = DeepSeekLLM(PROBE_CFG).probe_models(PROBE_KEY)

    assert result == ProbeResult(kind="ok", status_code=200, message="HTTP 200")
    assert len(record["requests"]) == 1  # 只发一次，不重试
    request = record["requests"][0]
    assert request.get_method() == "GET"
    assert request.full_url.endswith("/models")
    assert "/chat/completions" not in request.full_url
    assert request.data is None  # 空 payload：不为校验构造 messages
    assert request.get_header("Authorization") == f"Bearer {PROBE_KEY}"
    assert record["timeouts"] == [10]  # auth_check_timeout_s，不是 timeout_s


@pytest.mark.parametrize("code", [401, 403])
def test_probe_models_maps_auth_codes(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    """401 / 403 → auth，状态码原样带出，诊断里没有 Key。"""
    _patch_probe(monkeypatch, _raise_http(code))

    result = DeepSeekLLM(PROBE_CFG).probe_models(PROBE_KEY)

    assert result.kind == "auth"
    assert result.status_code == code
    assert PROBE_KEY not in result.message


@pytest.mark.parametrize("code", [400, 429, 500, 503])
def test_probe_models_maps_other_http_to_server(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    """其余 4xx / 5xx（含 429、503）→ server，不能误判成 Key 无效。"""
    _patch_probe(monkeypatch, _raise_http(code))

    result = DeepSeekLLM(PROBE_CFG).probe_models(PROBE_KEY)

    assert result.kind == "server"
    assert result.status_code == code
    assert f"HTTP {code}" in result.message


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("dns failure"),
        TimeoutError("timed out"),
        socket.gaierror(-2, "unknown host"),
        ConnectionError("refused"),
    ],
    ids=["urlerror", "timeout", "gaierror", "connection"],
)
def test_probe_models_maps_network_errors(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    """网络类异常一律 network，status_code 为空，诊断只放异常类名。"""
    _patch_probe(monkeypatch, _raise(error))

    result = DeepSeekLLM(PROBE_CFG).probe_models(PROBE_KEY)

    assert result.kind == "network"
    assert result.status_code is None
    assert result.message == type(error).__name__
    assert PROBE_KEY not in result.message


def test_probe_models_without_key_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """空 Key 不发请求，归入 auth。"""
    record = _patch_probe(monkeypatch, lambda: _FakeResponse(b"{}"))

    result = DeepSeekLLM(LLMConfig(api_key="")).probe_models("   ")

    assert result.kind == "auth" and result.status_code is None
    assert record["requests"] == []


def test_probe_models_honours_explicit_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """调用方显式传的超时优先于配置默认值。"""
    record = _patch_probe(monkeypatch, lambda: _FakeResponse(b"{}"))

    DeepSeekLLM(PROBE_CFG).probe_models(PROBE_KEY, timeout_s=2.5)

    assert record["timeouts"] == [2.5]


def test_generate_still_uses_inference_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """推理路径仍只读 timeout_s，不被校验超时影响（AC-24 的另一半）。"""
    body = b'{"choices": [{"message": {"content": "{\\"label\\": \\"x\\"}"}}]}'
    record = _patch_probe(monkeypatch, lambda: _FakeResponse(body))

    DeepSeekLLM(PROBE_CFG).generate("sys", "user")

    assert record["timeouts"] == [60]
    assert record["requests"][0].full_url.endswith("/chat/completions")


def test_probe_and_health_never_leak_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """校验失败时的 message 与 health 的 last_error 都不含 Key。"""
    _patch_probe(monkeypatch, _raise_http(401))
    llm = DeepSeekLLM(PROBE_CFG)

    result = llm.probe_models(PROBE_KEY)
    assert llm.health() is False
    assert PROBE_KEY not in result.message
    assert PROBE_KEY not in llm.last_error


def test_health_true_on_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """探针拿到 2xx 时 health 仍返回 bool True（签名与语义不变）。"""
    _patch_probe(monkeypatch, lambda: _FakeResponse(b'{"data": []}'))

    assert DeepSeekLLM(CFG).health() is True
