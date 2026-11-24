"""设置（规格 006 §2 / §3）：界面开关（所有角色）与模型 Key（管理员）。

模型设置沿用旧界面的全部安全口径：
- 响应里**绝无** Key、Key 长度或片段；状态行只说「是否配置 / 上次校验结果 / 来源」；
- `PUT` 先 `probe_models`，通过才 `set_key`，失败的输入不回显、不落盘；
- 清除只删 `auth.json` 里的 provider 条目。

写入口仍然只有 `services.credentials` 一处；本模块不直接碰凭据文件。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from fastapi import APIRouter, Depends

from ...adapters.embedding import DashScopeEmbedder, EmbeddingError
from ...adapters.llm import AUTH_FAILURE_CODES, DeepSeekLLM, ProbeResult
from ...config import load_config
from ...services.credentials import (
    DASHSCOPE_PROVIDER,
    DEFAULT_PROVIDER,
    CheckStatus,
    CredentialError,
    KeyStatus,
    clear_key,
    list_key_status,
    read_check_status,
    record_check_status,
    resolve_key,
    set_key,
)
from ..deps import RepoDep, require_capability
from ..errors import (
    MODEL_KEY_INVALID,
    MODEL_PROBE_FAILED,
    NOT_FOUND,
    STATE_CONFLICT,
    VALIDATION_ERROR,
    ApiError,
    make_action,
)
from ..schemas import (
    EmbeddingKeyIn,
    EmbeddingView,
    ModelCheckView,
    ModelKeyIn,
    ModelSettingsView,
    ProviderView,
    SettingsUiIn,
    SettingsUiView,
)

router = APIRouter()

#: 界面第三层开关的键名与取值口径（规格 005 §4）：只有 "1" 为开，读取不回写。
SHOW_QUERY_PATH_KEY = "ui.show_query_path"

PROVIDER_NAMES: dict[str, str] = {"deepseek": "DeepSeek"}
PROBE_MESSAGES: dict[str, str] = {
    "auth": "Key 不正确，请检查后重新输入",
    "network": "网络不通，请稍后重试（本次未保存）",
    "server": "服务暂时不可用，本次未保存",
}
STATUS_STALE_DAYS = 30


def probe_key(key: str) -> ProbeResult:
    """一次 `GET /models` 校验。测试打桩本函数即可完全不联网。"""
    return DeepSeekLLM(load_config().llm).probe_models(key)


#: 探测向量 Key 用的短文本：一条真实的检索式输入，与核对时的 `text_type` 一致
EMBEDDING_PROBE_TEXT = "劳动用工"


def probe_embedding(key: str) -> ProbeResult:
    """调一次真实的 embed（`text_type="query"`、一条短文本），成功即 `ok`。

    分类判据与 `probe_key` 同一套，只看状态码与异常类型、不看异常文本：
    401 / 403 → `auth`；其他 HTTP 状态码 → `server`；拿不到状态码（网络、超时、
    返回格式异常）→ `network`；调用成功 → `ok`。测试打桩本函数即可完全不联网。
    """
    token = (key or "").strip()
    if not token:
        return ProbeResult(kind="auth", message="未提供 API Key")
    cfg = replace(load_config().embedding, api_key=token)
    try:
        DashScopeEmbedder(cfg).embed([EMBEDDING_PROBE_TEXT], text_type="query")
    except EmbeddingError as exc:
        if exc.status_code is None:
            return ProbeResult(kind="network", message=type(exc).__name__)
        kind = "auth" if exc.status_code in AUTH_FAILURE_CODES else "server"
        return ProbeResult(kind=kind, status_code=exc.status_code, message=f"HTTP {exc.status_code}")
    return ProbeResult(kind="ok", status_code=200, message="HTTP 200")


# ---------------------------------------------------------------- 界面开关


@router.get(
    "/settings/ui",
    dependencies=[Depends(require_capability("settings_ui"))],
    response_model=SettingsUiView,
)
def get_settings_ui(repo: RepoDep) -> SettingsUiView:
    """非法旧值 / 缺键一律读成「关」，且不回写（与旧界面同一口径）。"""
    return SettingsUiView(show_query_path=repo.get_setting(SHOW_QUERY_PATH_KEY) == "1")


@router.put(
    "/settings/ui",
    dependencies=[Depends(require_capability("settings_ui"))],
    response_model=SettingsUiView,
)
def put_settings_ui(payload: SettingsUiIn, repo: RepoDep) -> SettingsUiView:
    repo.set_setting(SHOW_QUERY_PATH_KEY, "1" if payload.show_query_path else "0")
    return SettingsUiView(show_query_path=payload.show_query_path)


# ---------------------------------------------------------------- 模型 Key（管理员）


@router.get(
    "/settings/model",
    dependencies=[Depends(require_capability("settings_model"))],
    response_model=ModelSettingsView,
)
def get_settings_model() -> ModelSettingsView:
    """管理员读模型状态。响应里只有来源与校验结果，没有 Key。"""
    return _model_view()


@router.put(
    "/settings/model",
    dependencies=[Depends(require_capability("settings_model"))],
    response_model=ModelSettingsView,
)
def put_settings_model(payload: ModelKeyIn) -> ModelSettingsView:
    """测试并保存：校验不通过就不写文件、不写校验历史、不回显输入。"""
    provider = _known_provider(payload.provider)
    key = (payload.key or "").strip()
    if not key:
        raise ApiError(400, VALIDATION_ERROR, "请先粘贴 API Key", details={"fields": ["key"]})
    probe = probe_key(key)
    if probe.kind != "ok":
        if probe.kind == "auth":
            raise ApiError(
                422,
                MODEL_KEY_INVALID,
                PROBE_MESSAGES["auth"],
                action=make_action("open_settings", "重新填写 Key"),
            )
        raise ApiError(
            502,
            MODEL_PROBE_FAILED,
            PROBE_MESSAGES.get(probe.kind, PROBE_MESSAGES["server"]),
            action=make_action("retry", "重试"),
        )
    try:
        set_key(provider, key)
    except CredentialError as exc:
        # 异常消息只含 provider 与路径，不含 Key
        raise ApiError(409, STATE_CONFLICT, str(exc)) from exc
    try:
        record_check_status(provider, True, "ok")
    except (OSError, ValueError):
        # 校验历史是可丢的展示数据：写失败只降级为「未校验」，不回滚已成功的保存
        pass
    return _model_view()


@router.post(
    "/settings/model/{provider}/test",
    dependencies=[Depends(require_capability("settings_model"))],
    response_model=ModelSettingsView,
)
def test_settings_model(provider: str) -> ModelSettingsView:
    """复验**当前生效**的 Key：只发 `GET /models`，不改 `auth.json`，返回最新状态。"""
    provider = _known_provider(provider)
    resolved = resolve_key(provider)
    if resolved.source == "none":
        raise ApiError(409, STATE_CONFLICT, "尚未配置 Key，请先保存后再测试")
    probe = probe_key(resolved.key)
    try:
        record_check_status(provider, probe.kind == "ok", probe.kind)
    except (OSError, ValueError):
        pass
    return _model_view()


@router.delete(
    "/settings/model/{provider}",
    dependencies=[Depends(require_capability("settings_model"))],
    response_model=ModelSettingsView,
)
def delete_settings_model(provider: str) -> ModelSettingsView:
    """清除只删该 provider 条目（不写空串），返回清除后的最新状态。"""
    provider = _known_provider(provider)
    try:
        clear_key(provider)
    except CredentialError as exc:
        raise ApiError(409, STATE_CONFLICT, str(exc)) from exc
    return _model_view()


# ---------------------------------------------------------------- 向量检索 Key（管理员）
#
# provider 固定为 DashScope，所以路径里没有 `{provider}`；探测通过才落盘，失败不回显、
# 不写文件。响应复用 `ModelSettingsView`：前端一个模型就够，`embedding` 字段自带状态与
# 回退说明（不配 Key 仍然能核对，只是退化为关键词检索）。


@router.put(
    "/settings/embedding",
    dependencies=[Depends(require_capability("settings_model"))],
    response_model=ModelSettingsView,
)
def put_settings_embedding(payload: EmbeddingKeyIn) -> ModelSettingsView:
    """测试并保存向量检索 Key：探针不通过就不写文件、不回显输入。"""
    key = (payload.key or "").strip()
    if not key:
        raise ApiError(400, VALIDATION_ERROR, "请先粘贴 API Key", details={"fields": ["key"]})
    probe = probe_embedding(key)
    if probe.kind != "ok":
        if probe.kind == "auth":
            raise ApiError(
                422,
                MODEL_KEY_INVALID,
                PROBE_MESSAGES["auth"],
                action=make_action("open_settings", "重新填写 Key"),
            )
        raise ApiError(
            502,
            MODEL_PROBE_FAILED,
            PROBE_MESSAGES.get(probe.kind, PROBE_MESSAGES["server"]),
            action=make_action("retry", "重试"),
        )
    try:
        set_key(DASHSCOPE_PROVIDER, key)
    except CredentialError as exc:
        # 异常消息只含 provider 与路径，不含 Key
        raise ApiError(409, STATE_CONFLICT, str(exc)) from exc
    return _model_view()


@router.delete(
    "/settings/embedding",
    dependencies=[Depends(require_capability("settings_model"))],
    response_model=ModelSettingsView,
)
def delete_settings_embedding() -> ModelSettingsView:
    """清除向量检索 Key（幂等）：只删 `dashscope` 条目，不动大模型 Key。"""
    try:
        clear_key(DASHSCOPE_PROVIDER)
    except CredentialError as exc:
        raise ApiError(409, STATE_CONFLICT, str(exc)) from exc
    return _model_view()


# ---------------------------------------------------------------- 内部


def _known_provider(provider: str) -> str:
    known = [status.provider for status in list_key_status()]
    if provider not in known:
        raise ApiError(404, NOT_FOUND, f"未知的服务商：{provider}")
    return provider


def _model_view() -> ModelSettingsView:
    cfg = load_config()
    statuses = list_key_status()
    providers = [
        ProviderView(
            provider=status.provider,
            display_name=PROVIDER_NAMES.get(status.provider, status.provider),
            configured=status.source != "none",
            source=status.source,
            label=status.label,
        )
        for status in statuses
    ]
    provider = statuses[0].provider if statuses else DEFAULT_PROVIDER
    status = next((item for item in statuses if item.provider == provider), None)
    check = read_check_status(provider)
    embedding = cfg.embedding
    return ModelSettingsView(
        providers=providers,
        model=cfg.llm.model,
        status_line=_status_line(status, check, cfg.llm.model),
        check=None if check is None else _check_view(check),
        embedding=EmbeddingView(
            model=embedding.model,
            configured=bool(embedding.api_key),
            fallback_text="向量检索已启用" if embedding.api_key else "向量检索：未配置 Key，回退 BM25",
        ),
    )


def _check_view(check: CheckStatus) -> ModelCheckView:
    return ModelCheckView(
        ok=check.ok,
        checked_at=check.checked_at,
        kind=check.kind,
        stale=_is_stale(check.checked_at),
    )


def _is_stale(value: str) -> bool:
    """上次校验是否已超过 30 天：只影响提示，不改写「有效」。"""
    try:
        checked = datetime.fromisoformat(value).astimezone()
    except ValueError:
        return False
    return (datetime.now().astimezone() - checked).days > STATUS_STALE_DAYS


def _format_checked_at(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ""


def _status_line(status: KeyStatus | None, check: CheckStatus | None, model: str) -> str:
    """状态行：只答「是否已配置 / 上次校验是否通过 / 从哪来 / 哪个模型」。

    **本函数没有 Key 参数**，从签名上就做不到显示 Key、长度或片段。
    """
    provider = PROVIDER_NAMES.get(
        status.provider if status is not None else DEFAULT_PROVIDER, DEFAULT_PROVIDER
    )
    if status is None or status.source == "none":
        return f"未配置 · 服务商：{provider}"
    when = _format_checked_at(check.checked_at) if check is not None else ""
    if check is None or not when:
        head = f"已配置 · 未校验（来源：{status.label}）"
    elif check.ok:
        head = f"已配置 · 有效（{when}） · 来源：{status.label}"
    else:
        reason = PROBE_MESSAGES.get(check.kind, PROBE_MESSAGES["server"])
        head = f"已配置 · 未通过校验（{reason}） · 来源：{status.label}"
    line = f"{head} · 服务商：{provider} · 模型：{model}"
    if check is not None and when and _is_stale(check.checked_at):
        line = f"{line} · 上次校验超过 30 天，建议重新测试"
    return line
