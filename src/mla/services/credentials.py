"""凭据服务：全项目唯一的 Key 解析入口。

优先级（规格 002 第 4 章）：环境变量 > `<MLA_HOME>/auth.json` 的 provider 条目 > 未配置。
只有这两级：旧数据库 `settings.llm_api_key` 不再是解析来源（阶段四起由
`services.bootstrap.migrate_legacy_key()` 一次性导入并清理，见规格 002 第 7 章）。

本模块只做「取值 + 来源标签 + 面向调用方的读写 API」：不读 `.streamlit/secrets.toml`、
不做网络探测、不读数据库；UI、CLI、服务层与脚本都只能走这里。
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Literal

from ..adapters.auth_file import (  # noqa: F401  (CredentialError 对调用方重导出)
    DEFAULT_PROVIDER,
    STATUS_KINDS,
    CredentialError,
    auth_path,
    ensure_home_dir,
    get_entry,
    lock_dir,
    read_status,
    remove_entry,
    set_entry,
    write_status,
)
from ..config import Config, LLMConfig, load_config, mla_home

__all__ = [
    "CheckStatus",
    "CredentialError",
    "DASHSCOPE_PROVIDER",
    "DEFAULT_PROVIDER",
    "KeySource",
    "KeyStatus",
    "ResolvedKey",
    "clear_key",
    "home_lock",
    "list_key_status",
    "read_check_status",
    "record_check_status",
    "resolve_key",
    "resolve_llm_config",
    "set_key",
]

SOURCE_ENVIRONMENT = "environment"
SOURCE_AUTH_FILE = "auth_file"
SOURCE_NONE = "none"

# 来源标识的合法取值（规格 002 第 4 章）：只有两级来源 + 未配置
KeySource = Literal["environment", "auth_file", "none"]

# 服务商 → 环境变量名。本版大模型只有 DeepSeek；加第二家只需在这里多一行。
_ENV_VARS: dict[str, str] = {"deepseek": "MLA_LLM_API_KEY"}

#: 向量检索用的 provider：阿里云百炼（DashScope）。它**不是**大模型服务商，
#: 只服务 embedding Key，所以不进 `_ENV_VARS` —— 那张表同时是 `list_key_status()`
#: 与设置页「服务商」下拉的数据源，混进来会让 DashScope 出现在大模型 Key 的下拉里。
DASHSCOPE_PROVIDER = "dashscope"

#: 非大模型 provider → 环境变量名。与 `auth.json` 共用同一份文件与同一套读写 API。
_EMBEDDING_ENV_VARS: dict[str, str] = {DASHSCOPE_PROVIDER: "DASHSCOPE_API_KEY"}

_LABEL_AUTH_FILE = "用户凭据文件（auth.json）"
_LABEL_NONE = "未配置"


@dataclass(frozen=True)
class ResolvedKey:
    """解析结果。Key 不参与 repr，避免日志 / 调试输出带出明文。"""

    key: str = field(default="", repr=False)
    source: KeySource = SOURCE_NONE
    label: str = _LABEL_NONE
    origin: str = ""


@dataclass(frozen=True)
class KeyStatus:
    """面向界面的凭据概况：只回答存在性 / 来源 / 类型 / 路径，不含 Key 与长度。"""

    provider: str
    type: str
    source: str
    label: str
    path: str


@dataclass(frozen=True)
class CheckStatus:
    """最近一次 `GET /models` 校验结果（来自 `auth-status.json`）。"""

    ok: bool
    checked_at: str
    kind: str


def _home(home: Path | None) -> Path:
    return mla_home() if home is None else home


def _env_var(provider: str) -> str:
    """provider 对应的环境变量名；大模型与向量检索用同一套查询口径。"""
    return _ENV_VARS.get(provider) or _EMBEDDING_ENV_VARS.get(provider, "")


def _env_label(provider: str) -> str:
    variable = _env_var(provider)
    return f"环境变量（{variable}）" if variable else "环境变量"


def resolve_key(
    provider: str = DEFAULT_PROVIDER,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> ResolvedKey:
    """按 env > auth_file > none 返回第一个非空值；损坏的凭据文件显式报错。

    只读：不创建 `auth.json`、不建 `MLA_HOME` 目录、不碰任何数据库。
    """
    e = os.environ if env is None else env
    variable = _env_var(provider)
    if variable:
        value = (e.get(variable) or "").strip()
        if value:
            return ResolvedKey(
                key=value, source=SOURCE_ENVIRONMENT, label=_env_label(provider), origin=variable
            )
    root = _home(home)
    entry = get_entry(root, provider)
    if entry is not None:
        return ResolvedKey(
            key=str(entry["key"]),
            source=SOURCE_AUTH_FILE,
            label=_LABEL_AUTH_FILE,
            origin=str(auth_path(root)),
        )
    return ResolvedKey(source=SOURCE_NONE, label=_LABEL_NONE)


def resolve_llm_config(
    cfg: Config | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[LLMConfig | None, ResolvedKey]:
    """无 Key 返回 `(None, ResolvedKey.none)`；有 Key 返回已注入 Key 的 `LLMConfig`。"""
    resolved = resolve_key(DEFAULT_PROVIDER, env=env, home=home)
    if resolved.source == SOURCE_NONE:
        return None, resolved
    base = cfg if cfg is not None else load_config(env)
    return replace(base.llm, api_key=resolved.key), resolved


def _set_key_unlocked(provider: str, key: str, *, home: Path) -> None:
    """`set_key()` 的无锁内核：**调用方必须已持有 `home_lock(home)`**。

    仅供 `set_key()` 与首次迁移（`services.bootstrap`）使用：迁移要在同一把锁里完成
    「写入 → 重读校验 → 删旧行 → 写标记」，再调会自己抢锁的 `set_key()` 会自锁。
    """
    set_entry(home, provider, key)


def set_key(provider: str, key: str, *, home: Path | None = None) -> None:
    """保存 Key：返回前必须已完成落盘、权限收紧与重读校验，否则抛 `CredentialError`。"""
    root = _home(home)
    with home_lock(root):
        _set_key_unlocked(provider, key, home=root)


def clear_key(provider: str, *, home: Path | None = None) -> None:
    """清除某个 provider 的 Key；不存在时静默返回（幂等），不动其他 provider。"""
    root = _home(home)
    with home_lock(root):
        remove_entry(root, provider)


def list_key_status(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[KeyStatus]:
    """列出各 provider 的来源概况；结果里没有 Key，也没有 Key 长度。"""
    root = _home(home)
    path = str(auth_path(root))
    statuses: list[KeyStatus] = []
    for provider in sorted(_ENV_VARS):
        resolved = resolve_key(provider, env=env, home=root)
        statuses.append(
            KeyStatus(
                provider=provider,
                type="api_key" if resolved.source != SOURCE_NONE else "",
                source=resolved.source,
                label=resolved.label,
                path=path,
            )
        )
    return statuses


def record_check_status(
    provider: str,
    ok: bool,
    kind: str,
    checked_at: str | None = None,
    *,
    home: Path | None = None,
) -> None:
    """记录一次针对当前生效 Key 的校验结果；写失败抛 `OSError`，由调用方降级显示。"""
    if kind not in STATUS_KINDS:
        raise ValueError(f"未知的校验分类：{kind}")
    timestamp = checked_at or datetime.now().astimezone().isoformat(timespec="seconds")
    root = _home(home)
    data = read_status(root)
    data[provider] = {"ok": bool(ok), "checked_at": timestamp, "kind": kind}
    write_status(root, data)


def read_check_status(provider: str, *, home: Path | None = None) -> CheckStatus | None:
    """读最近一次校验结果；没有或文件损坏时返回 `None`（降级为「未校验」）。"""
    entry = read_status(_home(home)).get(provider)
    if entry is None:
        return None
    return CheckStatus(ok=bool(entry["ok"]), checked_at=str(entry["checked_at"]), kind=str(entry["kind"]))


@contextmanager
def home_lock(home: Path) -> Iterator[None]:
    """按 `MLA_HOME` 串行化配置写操作：原子 `mkdir` 抢锁，已存在即报错、不自动抢。

    `set_key` / `clear_key` 与首次凭据迁移（`services.bootstrap`）共用这把锁：
    迁移要在同一把锁里完成「写入 → 重读校验 → 删旧行 → 写标记」，所以它调的是
    `_set_key_unlocked()` 而不是会自己抢锁的 `set_key()`。正常与异常路径都在
    `finally` 里释放；发现遗留锁时只报诊断，绝不按等待时长自动判死抢锁。
    """
    ensure_home_dir(home)
    lock = lock_dir(home)
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise CredentialError(f"另一项配置操作可能正在进行（锁目录已存在：{lock}），请稍后重试") from exc
    except OSError as exc:
        raise CredentialError(f"无法创建锁目录，未写入：{lock}") from exc
    try:
        yield
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass
