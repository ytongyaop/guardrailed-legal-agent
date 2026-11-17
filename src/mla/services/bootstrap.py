"""运行期引导：默认业务库落到 `<MLA_HOME>`，并在首次切换时复制旧库；随后做一次性凭据迁移。

规格 002 第 5 章「对已有用户的兼容策略」：
- 显式 `MLA_DB` 尊重用户指定，**绝不自动搬迁**；相对路径按原语义（相对当前工作目录）支持并记警告；
- `<MLA_HOME>/mla.sqlite3` 已存在就以它为准，绝不与旧库合并；
- 新库不存在且当前工作目录有旧 `mla.sqlite3` 时，用标准库 `sqlite3.Connection.backup()` 做
  一致性复制，旧库文件保留；只做整库复制，不做表级合并；
- 两者都不存在时只返回新路径：**不建目录、不建文件**（单纯查询 Key 不应创建业务数据库）；
- 不扫描其他目录，只看当前工作目录这一处旧库。

规格 002 第 7 章「迁移方案」（阶段四，破坏性）：
- 候选来源只有旧数据库 `settings.llm_api_key`（secrets 不参与）；
- 迁移只执行一次：`<MLA_HOME>/auth-migration.json` 里 `{"legacy_llm_api_key": "complete"}`
  是唯一的完成标记，它只存状态，不含 Key、不含旧库路径；
- 写入 `auth.json` 后必须重读校验（值一致 + 来源 `auth_file`），**校验通过才**删除旧行并写标记；
- 失败一律显式上报（状态 `failed` + 可操作提示），不假装成功、不静默删除旧数据。

全部诊断只含路径，不含任何 Key 或 Key 长度。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from ..adapters.auth_file import DEFAULT_PROVIDER, CredentialError, auth_path, get_entry
from ..config import mla_home
from .credentials import SOURCE_AUTH_FILE, _set_key_unlocked, home_lock, resolve_key

DB_FILENAME = "mla.sqlite3"

# 迁移标记文件与键名（规格 002 第 3 章：只存状态）
MIGRATION_FILE = "auth-migration.json"
MIGRATION_FLAG = "legacy_llm_api_key"
# 旧数据库里的 Key 键名（旧 UI 写入 settings 表的键）
LEGACY_SETTING_KEY = "llm_api_key"

# 迁移状态机（规格 002 第 7 章）
STATUS_SKIPPED = "skipped"
STATUS_NO_CANDIDATE = "no_candidate"
STATUS_IMPORTED = "imported"
STATUS_ALREADY_CONFIGURED = "already_configured"
STATUS_FAILED = "failed"

# 面向用户的固定文案（不含 Key、不含长度，规格 002 第 7 章失败处理表）
MSG_READ_FAILED = "旧凭据读取失败，请重新保存 Key（旧数据保留，未做任何改动）"
MSG_WRITE_FAILED = "凭据迁移失败，请重新保存 Key（旧数据保留，未删除旧凭据）"
MSG_CLEANUP_FAILED = "旧凭据尚未清理（旧数据库仍含 Key 记录，下次启动会重试）"
MSG_MARKER_FAILED = "迁移完成但状态标记写入失败，下次启动会自动补记"
MSG_LOCKED = "另一项配置操作正在进行，本次未执行凭据迁移，请稍后重试"
MSG_CORRUPT = "用户凭据文件已损坏，凭据迁移未执行；请在设置页重新保存 Key（不会自动覆盖或生成备份）"
MSG_IMPORTED = "已从旧配置迁移到用户凭据文件"
MSG_ALREADY_CONFIGURED = "沿用已有的用户凭据文件（未覆盖），旧库中的 Key 记录已清理"

__all__ = [
    "BootstrapReport",
    "DB_FILENAME",
    "LEGACY_SETTING_KEY",
    "MIGRATION_FILE",
    "MIGRATION_FLAG",
    "MigrationReport",
    "ensure_runtime_ready",
    "migrate_legacy_key",
]


@dataclass(frozen=True)
class MigrationReport:
    """一次凭据迁移的结果。

    - `status`：`skipped` / `no_candidate` / `imported` / `already_configured` / `failed`；
    - `messages`：面向用户的诊断，**不含 Key、不含 Key 长度**；空列表表示无需提示。
    """

    status: str
    messages: list[str]


@dataclass(frozen=True)
class BootstrapReport:
    """一次引导的结果。

    - `db_path`：本次应使用的数据库路径；
    - `copied_from`：发生一致性复制时的旧库路径，否则为 `None`；
    - `created_dir`：本次是否创建了 `MLA_HOME` 目录；
    - `warnings`：不含 Key 的诊断（相对路径覆盖、复制失败等）；
    - `migration`：首次凭据迁移结果（见 `MigrationReport`）。
    """

    db_path: str
    copied_from: str | None
    created_dir: bool
    warnings: list[str]
    migration: MigrationReport


def ensure_runtime_ready(*, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> BootstrapReport:
    """确定本次运行该用哪个数据库，必要时一次性复制旧库并完成首次凭据迁移；可重复调用（幂等）。

    `env` / `cwd` 是测试注入点：生产默认取进程环境与当前工作目录。
    """
    e = os.environ if env is None else env
    base = Path.cwd() if cwd is None else cwd
    home = mla_home(e)
    target = home / DB_FILENAME
    legacy = base / DB_FILENAME
    warnings: list[str] = []

    configured = (e.get("MLA_DB") or "").strip()
    if configured:
        # 规则 3：显式路径不搬迁数据库，但仍允许凭据迁移从该库读取旧 Key
        db_path, warnings = _explicit_db(configured, base)
        candidates = [Path(db_path)]
    elif target.exists():
        # 规则 1 / 4：新库已存在就以它为准，旧库不参与业务数据合并，只作旧 Key 候选
        db_path, candidates = str(target), [target, legacy]
    elif not legacy.exists() or _same_path(legacy, target):
        # 规则 5 / 无旧库：只返回新路径，不建目录、不建空库
        db_path, candidates = str(target), [target]
    else:
        created_dir = not home.exists()
        try:
            _copy_database(legacy, target)
        except (OSError, sqlite3.Error) as exc:
            # 复制失败不制造空库：本次继续用旧库，让账号与业务数据仍然可见
            warnings.append(f"旧数据库复制失败，本次继续使用旧库 {legacy}：{exc}")
            db_path, copied_from, candidates = str(legacy), None, [legacy]
        else:
            # 复制成功后新旧两处都要清理旧 Key（规格 002 第 7 章成功流程第 4 条）
            db_path, copied_from, candidates = str(target), str(legacy), [target, legacy]
        migration = migrate_legacy_key(home, candidates, env=e)
        return BootstrapReport(db_path, copied_from, created_dir, warnings, migration)

    migration = migrate_legacy_key(home, candidates, env=e)
    return BootstrapReport(db_path, None, False, warnings, migration)


def _explicit_db(configured: str, base: Path) -> tuple[str, list[str]]:
    """规则 3：显式 `MLA_DB` 直接使用，不复制、不建目录、不建文件。"""
    path = Path(configured)
    if path.is_absolute():
        return configured, []
    warnings = [f"MLA_DB 使用了相对路径 {configured}，行为与旧版一致（相对当前工作目录），建议改为绝对路径"]
    return str(base / path), warnings


def _same_path(left: Path, right: Path) -> bool:
    """源与目标是否同一文件：用户把 MLA_HOME 设为当前目录时不能自己覆盖自己。"""
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _copy_database(source: Path, target: Path) -> None:
    """用 `backup()` 复制到同目录临时文件再原子替换：失败不会留下半个新库。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f"{target.name}.{os.getpid()}.copy")
    try:
        with closing(sqlite3.connect(source.absolute().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(str(temp))) as dst:
                src.backup(dst)
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()


# ---------------------------------------------------------------- 首次凭据迁移


def migrate_legacy_key(
    home: Path, db_paths: Sequence[Path], *, env: Mapping[str, str] | None = None
) -> MigrationReport:
    """把旧数据库里的 `settings.llm_api_key` 一次性导入 `auth.json` 并清理旧行。

    顺序与分支严格对应规格 002 第 7 章：
    1. 已有 `complete` 标记 → `skipped`，**不再读旧库**（用户之后清除 `auth.json` 也不会回迁）；
    2. `auth.json` 已有合法 `deepseek` → 不覆盖，只清理旧行 + 写标记 → `already_configured`；
    3. 没有文件凭据但旧库有非空 Key → 在 `home_lock` 内写入、重读校验，通过后才删旧行、写标记；
    4. 没有旧库 / 旧库无 Key → `no_candidate`，不建空凭据、不写标记；
    5. 任何一步失败 → `failed` + 固定提示，旧数据保留。

    `env` 只为与 `ensure_runtime_ready()` 同形保留：迁移不把环境变量当来源，也不落盘它。
    """
    if _migration_complete(home):
        return MigrationReport(STATUS_SKIPPED, [])

    candidates = _candidate_dbs(db_paths)
    try:
        configured = get_entry(home, DEFAULT_PROVIDER) is not None
    except CredentialError:
        # 损坏的文件不自动改名、不备份、不覆盖：管理员显式重新保存后才可替换
        return MigrationReport(STATUS_FAILED, [_with_path(MSG_CORRUPT, auth_path(home))])

    legacy_key = ""
    if not configured:
        try:
            legacy_key = _first_legacy_key(candidates)
        except (OSError, sqlite3.Error):
            # 读不出来 ≠ 旧库没有 Key：绝不当成"无 Key"并标记完成
            return MigrationReport(STATUS_FAILED, [MSG_READ_FAILED])
        if not legacy_key:
            return MigrationReport(STATUS_NO_CANDIDATE, [])

    try:
        with home_lock(home):
            return _apply_locked(home, candidates, legacy_key)
    except CredentialError:
        # 只有抢锁失败会走到这里（写入 / 校验失败已在 _apply_locked 内转成 failed）
        return MigrationReport(STATUS_FAILED, [MSG_LOCKED])


def _apply_locked(home: Path, candidates: Sequence[Path], legacy_key: str) -> MigrationReport:
    """持锁执行：导入（必要时）→ 删旧行 → 写标记。全程不打印任何 Key。"""
    try:
        configured = get_entry(home, DEFAULT_PROVIDER) is not None
    except CredentialError:
        return MigrationReport(STATUS_FAILED, [_with_path(MSG_CORRUPT, auth_path(home))])

    if not configured:
        if not legacy_key:
            return MigrationReport(STATUS_NO_CANDIDATE, [])
        try:
            _set_key_unlocked(DEFAULT_PROVIDER, legacy_key, home=home)
            resolved = resolve_key(DEFAULT_PROVIDER, env={}, home=home)
        except CredentialError:
            return MigrationReport(STATUS_FAILED, [MSG_WRITE_FAILED])
        if resolved.source != SOURCE_AUTH_FILE or resolved.key != legacy_key:
            # 重读校验失败：不删旧行、不写标记
            return MigrationReport(STATUS_FAILED, [MSG_WRITE_FAILED])

    try:
        _delete_legacy_keys(candidates)
    except (OSError, sqlite3.Error):
        # 已验证的新文件可继续使用，但旧行还在：不写标记，下次重试清理
        return MigrationReport(STATUS_FAILED, [MSG_CLEANUP_FAILED])

    try:
        _write_marker(home)
    except OSError:
        # 新文件保留；下次看到已有合法文件凭据时只补清理与标记，不重复覆盖
        return MigrationReport(STATUS_FAILED, [MSG_MARKER_FAILED])

    if configured:
        return MigrationReport(
            STATUS_ALREADY_CONFIGURED, [_with_path(MSG_ALREADY_CONFIGURED, auth_path(home))]
        )
    return MigrationReport(STATUS_IMPORTED, [_with_path(MSG_IMPORTED, auth_path(home))])


def _with_path(message: str, path: Path) -> str:
    return f"{message}：{path}"


def _candidate_dbs(db_paths: Sequence[Path]) -> list[Path]:
    """只保留**实际存在**的库文件，并按解析后的绝对路径去重（同一库不读两遍）。"""
    seen: set[str] = set()
    result: list[Path] = []
    for raw in db_paths:
        path = Path(raw)
        if not path.is_file():
            continue
        try:
            identity = os.path.normcase(str(path.resolve()))
        except OSError:
            identity = os.path.normcase(os.path.abspath(path))
        if identity in seen:
            continue
        seen.add(identity)
        result.append(path)
    return result


def _read_legacy_key(path: Path) -> str:
    """只读打开旧库取 `settings.llm_api_key`。

    - 没有 `settings` 表 → 空串（该库只是"旧配置容器但没有 Key"）；
    - 其他读取错误（不是合法 SQLite、被占用、权限）→ **抛出**，由调用方转成 `failed`。
    """
    uri = path.absolute().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        if not _has_settings_table(conn):
            return ""
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (LEGACY_SETTING_KEY,)).fetchone()
    return "" if row is None else str(row[0] or "").strip()


def _has_settings_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'settings'").fetchone()
    return row is not None


def _first_legacy_key(candidates: Sequence[Path]) -> str:
    """按候选顺序取第一处非空旧 Key；所有库都要读一遍以暴露读取失败。"""
    found = ""
    for path in candidates:
        value = _read_legacy_key(path)
        if value and not found:
            found = value
    return found


def _delete_legacy_keys(candidates: Sequence[Path]) -> None:
    """删除所有候选库里的 `llm_api_key` 行：可重复执行，其他 settings 与表不动。"""
    for path in candidates:
        with closing(sqlite3.connect(str(path))) as conn:
            with conn:
                if not _has_settings_table(conn):
                    continue
                conn.execute("DELETE FROM settings WHERE key = ?", (LEGACY_SETTING_KEY,))


def _migration_complete(home: Path) -> bool:
    """读完成标记。文件缺失、损坏或形状不对一律当"未迁移"（规格 002 第 7 章）。"""
    try:
        data = json.loads((home / MIGRATION_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get(MIGRATION_FLAG) == "complete"


def _write_marker(home: Path) -> None:
    """原子写完成标记：同目录临时文件 + `os.replace`；失败抛 `OSError`。"""
    payload = json.dumps({MIGRATION_FLAG: "complete"}, ensure_ascii=False) + "\n"
    home.mkdir(parents=True, exist_ok=True)
    tmp: Path | None = None
    try:
        fd, raw = tempfile.mkstemp(prefix=f".{MIGRATION_FILE}.", suffix=".tmp", dir=str(home))
        tmp = Path(raw)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, home / MIGRATION_FILE)
        tmp = None
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
