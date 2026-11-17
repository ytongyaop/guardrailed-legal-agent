"""运行期目录引导：默认业务库落到 `<MLA_HOME>`，并在首次切换时复制旧工作目录里的库。

规格 002 第 5 章「对已有用户的兼容策略」：
- 显式 `MLA_DB` 尊重用户指定，**绝不自动搬迁**；相对路径按原语义（相对当前工作目录）支持并记警告；
- `<MLA_HOME>/mla.sqlite3` 已存在就以它为准，绝不与旧库合并；
- 新库不存在且当前工作目录有旧 `mla.sqlite3` 时，用标准库 `sqlite3.Connection.backup()` 做
  一致性复制，旧库文件保留；只做整库复制，不做表级合并；
- 两者都不存在时只返回新路径：**不建目录、不建文件**（单纯查询 Key 不应创建业务数据库）；
- 不扫描其他目录，只看当前工作目录这一处旧库。

全部诊断只含路径，不含任何 Key。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from ..config import mla_home

DB_FILENAME = "mla.sqlite3"

__all__ = ["BootstrapReport", "DB_FILENAME", "ensure_runtime_ready"]


@dataclass(frozen=True)
class BootstrapReport:
    """一次引导的结果。

    - `db_path`：本次应使用的数据库路径；
    - `copied_from`：发生一致性复制时的旧库路径，否则为 `None`；
    - `created_dir`：本次是否创建了 `MLA_HOME` 目录；
    - `warnings`：不含 Key 的诊断（相对路径覆盖、复制失败等）。
    """

    db_path: str
    copied_from: str | None
    created_dir: bool
    warnings: list[str]


def ensure_runtime_ready(*, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> BootstrapReport:
    """确定本次运行该用哪个数据库，必要时一次性复制旧库；可重复调用（幂等）。

    `env` / `cwd` 是测试注入点：生产默认取进程环境与当前工作目录。
    """
    e = os.environ if env is None else env
    base = Path.cwd() if cwd is None else cwd

    configured = (e.get("MLA_DB") or "").strip()
    if configured:
        return _explicit_db(configured, base)

    home = mla_home(e)
    target = home / DB_FILENAME
    warnings: list[str] = []
    if target.exists():
        # 规则 1 / 4：新库已存在就以它为准，旧库不参与业务数据合并
        return BootstrapReport(str(target), None, False, warnings)

    legacy = base / DB_FILENAME
    if not legacy.exists() or _same_path(legacy, target):
        # 规则 5 / 无旧库：只返回新路径，不建目录、不建空库
        return BootstrapReport(str(target), None, False, warnings)

    created_dir = not home.exists()
    try:
        _copy_database(legacy, target)
    except (OSError, sqlite3.Error) as exc:
        # 复制失败不制造空库：本次继续用旧库，让账号与业务数据仍然可见
        warnings.append(f"旧数据库复制失败，本次继续使用旧库 {legacy}：{exc}")
        return BootstrapReport(str(legacy), None, False, warnings)
    return BootstrapReport(str(target), str(legacy), created_dir, warnings)


def _explicit_db(configured: str, base: Path) -> BootstrapReport:
    """规则 3：显式 `MLA_DB` 直接使用，不复制、不建目录、不建文件。"""
    warnings: list[str] = []
    path = Path(configured)
    if path.is_absolute():
        return BootstrapReport(configured, None, False, warnings)
    warnings.append(
        f"MLA_DB 使用了相对路径 {configured}，行为与旧版一致（相对当前工作目录），建议改为绝对路径"
    )
    return BootstrapReport(str(base / path), None, False, warnings)


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
