"""凭据服务：全项目唯一的 Key 解析入口。

优先级（规格 002 第 4 章）：环境变量 > `<MLA_HOME>/auth.json` 的 provider 条目 > 未配置。
**阶段三临时兜底**：旧数据库 `settings.llm_api_key` 作为最低优先级只读来源
（`legacy_database` / `兼容来源（旧数据库）`），排在 `auth_file` 之后；阶段四随迁移删除。

本模块只做「取值 + 来源标签 + 面向调用方的读写 API」：不读 `.streamlit/secrets.toml`、
不做网络探测；UI、CLI、服务层与脚本都只能走这里。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
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
    "DEFAULT_PROVIDER",
    "KeyStatus",
    "ResolvedKey",
    "clear_key",
    "list_key_status",
    "read_check_status",
    "record_check_status",
    "resolve_key",
    "resolve_llm_config",
    "set_key",
]

SOURCE_ENVIRONMENT = "environment"
SOURCE_AUTH_FILE = "auth_file"
SOURCE_LEGACY_DATABASE = "legacy_database"
SOURCE_NONE = "none"

# 来源标识的合法取值：阶段三是四种，阶段四删掉 legacy_database 后回到三种
KeySource = Literal["environment", "auth_file", "legacy_database", "none"]

# 服务商 → 环境变量名。本版只有 DeepSeek；加第二家只需在这里多一行。
_ENV_VARS: dict[str, str] = {"deepseek": "MLA_LLM_API_KEY"}

# 旧数据库里的 Key 键名（与旧 UI 写入 settings 表的键一致）
LEGACY_SETTING_KEY = "llm_api_key"

_LABEL_AUTH_FILE = "用户凭据文件（auth.json）"
_LABEL_LEGACY_DATABASE = "兼容来源（旧数据库）"
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


def _env_label(provider: str) -> str:
    variable = _ENV_VARS.get(provider, "")
    return f"环境变量（{variable}）" if variable else "环境变量"


def resolve_key(
    provider: str = DEFAULT_PROVIDER,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    legacy_db: str | Path | None = None,
) -> ResolvedKey:
    """按 env > auth_file > legacy_database > none 返回第一个非空值；损坏的凭据文件显式报错。

    `legacy_db` 是**阶段三临时兜底**的旧数据库路径（通常传 `cfg.paths.db`）：只在文件
    已存在时以只读方式读 `settings.llm_api_key`，缺失或读失败一律当空，绝不创建数据库。
    """
    e = os.environ if env is None else env
    variable = _ENV_VARS.get(provider, "")
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
    legacy = _legacy_database_key(legacy_db, provider)
    if legacy:
        return ResolvedKey(
            key=legacy,
            source=SOURCE_LEGACY_DATABASE,
            label=_LABEL_LEGACY_DATABASE,
            origin=str(Path(legacy_db)) if legacy_db is not None else "",
        )
    return ResolvedKey(source=SOURCE_NONE, label=_LABEL_NONE)


def _legacy_database_key(db: str | Path | None, provider: str) -> str:
    """阶段三兜底：只读打开**已存在**的旧库取 `settings.llm_api_key`。

    规格 002 第 4 章：文件不存在、表 / 键缺失、读取失败都当作空（继续下探到 none），
    不报错、不创建数据库；阶段四删除本函数与 `legacy_database` 来源。只有默认的
    `deepseek` 有这段历史（其他 provider 从来没有写进过旧库）。
    """
    if db is None or provider != DEFAULT_PROVIDER:
        return ""
    path = Path(db)
    if not path.is_file():
        return ""
    try:
        uri = path.absolute().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (LEGACY_SETTING_KEY,)).fetchone()
    except (OSError, sqlite3.Error):
        # 旧库不是合法 SQLite、缺表、被占用：按「没有旧 Key」处理，不阻断解析
        return ""
    return "" if row is None else str(row[0] or "").strip()


def resolve_llm_config(
    cfg: Config | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    legacy_db: str | Path | None = None,
) -> tuple[LLMConfig | None, ResolvedKey]:
    """无 Key 返回 `(None, ResolvedKey.none)`；有 Key 返回已注入 Key 的 `LLMConfig`。"""
    resolved = resolve_key(DEFAULT_PROVIDER, env=env, home=home, legacy_db=legacy_db)
    if resolved.source == SOURCE_NONE:
        return None, resolved
    base = cfg if cfg is not None else load_config(env)
    return replace(base.llm, api_key=resolved.key), resolved


def set_key(provider: str, key: str, *, home: Path | None = None) -> None:
    """保存 Key：返回前必须已完成落盘、权限收紧与重读校验，否则抛 `CredentialError`。"""
    root = _home(home)
    with _home_lock(root):
        set_entry(root, provider, key)


def clear_key(provider: str, *, home: Path | None = None) -> None:
    """清除某个 provider 的 Key；不存在时静默返回（幂等），不动其他 provider。"""
    root = _home(home)
    with _home_lock(root):
        remove_entry(root, provider)


def list_key_status(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    legacy_db: str | Path | None = None,
) -> list[KeyStatus]:
    """列出各 provider 的来源概况；结果里没有 Key，也没有 Key 长度。

    `legacy_db` 与 `resolve_key()` 同义：阶段三界面可能显示「兼容来源（旧数据库）」标签，
    但本批不改 `ui/app.py`，这里只是把接口备好；阶段四随兜底一起删掉这个参数。
    """
    root = _home(home)
    path = str(auth_path(root))
    statuses: list[KeyStatus] = []
    for provider in sorted(_ENV_VARS):
        resolved = resolve_key(provider, env=env, home=root, legacy_db=legacy_db)
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
def _home_lock(home: Path) -> Iterator[None]:
    """按 `MLA_HOME` 串行化配置写操作：原子 `mkdir` 抢锁，已存在即报错、不自动抢。

    当前只有 `set_key` / `clear_key` 用它；正常与异常路径都在 `finally` 里释放。
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
