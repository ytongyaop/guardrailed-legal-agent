"""凭据文件适配：`auth.json` / `auth-status.json` 的 JSON 校验、原子写入与平台权限。

职责边界（规格 002 第 6 章）：只管文件。不读环境变量、不读数据库、不 import 界面框架。
只有本模块知道凭据文件的形状、权限收紧和原子替换细节；上层一律走 `services.credentials`。

错误口径（规格 002 第 3、4 章）：`auth.json` 损坏必须显式报错（`CredentialError`），
消息里只有 provider 与路径，绝不包含 Key、Key 长度或任何片段；`auth-status.json` 是
可丢的展示数据，读坏了一律降级成 `{}`，不抛错。
"""

from __future__ import annotations

import json
import locale
import os
import stat
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

# 本版唯一的内置服务商；`auth.json` 的顶层键名就是它
DEFAULT_PROVIDER = "deepseek"

# 校验历史的分类枚举（规格 002 第 3 章），与界面固定文案一一对应
STATUS_KINDS: frozenset[str] = frozenset({"ok", "auth", "network", "server"})

# Windows ACL 里不允许出现的宽泛主体：英文名、中文名与已知 SID 都算
_BROAD_ACL_SUBJECTS = (
    "everyone",
    "builtin\\users",
    "所有人",
    "s-1-1-0",  # Everyone
    "s-1-5-32-545",  # BUILTIN\Users
)

# 凭据条目唯一接受的类型：本版不实现 OAuth / 其他凭据种类
_CREDENTIAL_TYPE = "api_key"


class CredentialError(Exception):
    """凭据文件错误。消息只描述 provider 与路径，绝不携带 Key 内容。"""


def _is_windows() -> bool:
    """平台判定单独成函数：生产口径就是 `os.name`，测试可替换分支。"""
    return os.name == "nt"


def auth_path(home: Path) -> Path:
    """凭据文件路径：`<MLA_HOME>/auth.json`。"""
    return home / "auth.json"


def status_path(home: Path) -> Path:
    """校验历史路径：`<MLA_HOME>/auth-status.json`（不含 Key，可丢）。"""
    return home / "auth-status.json"


def lock_dir(home: Path) -> Path:
    """配置互斥锁目录：`<MLA_HOME>/auth.lock`（原子 mkdir 实现，不用第三方锁）。"""
    return home / "auth.lock"


def ensure_home_dir(home: Path) -> None:
    """创建用户目录；非 Windows 顺带收紧到 0700 并校验。

    只在真正要写文件时调用：只读解析不得创建任何目录。
    """
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CredentialError(f"无法创建用户目录：{home}") from exc
    if _is_windows():
        # Windows 上 chmod 只影响只读属性，目录隔离交给默认 ACL；不递归改目录 ACL
        return
    try:
        os.chmod(home, 0o700)
        mode = stat.S_IMODE(home.stat().st_mode)
    except OSError as exc:
        raise CredentialError(f"无法收紧用户目录权限：{home}") from exc
    if mode != 0o700:
        raise CredentialError(f"用户目录权限不是 0700（实际 {oct(mode)}）：{home}")


def read_auth(home: Path) -> dict[str, Any]:
    """读凭据文件。不存在返回 `{}`；存在但损坏 → `CredentialError`（绝不当空文件覆盖）。"""
    path = auth_path(home)
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CredentialError(f"读取凭据文件失败：{path}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CredentialError(f"凭据文件不是合法 JSON：{path}") from exc
    if not isinstance(data, dict):
        raise CredentialError(f"凭据文件顶层必须是 JSON 对象：{path}")
    for provider, entry in data.items():
        _validate_entry(provider, entry)
    return data


def write_auth(home: Path, data: dict[str, Any]) -> None:
    """整体写入凭据文件：先校验形状，再原子替换。"""
    if not isinstance(data, dict):
        raise CredentialError("凭据内容必须是 JSON 对象")
    for provider, entry in data.items():
        _validate_entry(provider, entry)
    ensure_home_dir(home)
    _atomic_write_json(auth_path(home), data)


def get_entry(home: Path, provider: str) -> dict[str, Any] | None:
    """取某个 provider 的凭据条目（副本）；没有则 `None`。"""
    entry = read_auth(home).get(provider)
    return None if entry is None else dict(entry)


def set_entry(home: Path, provider: str, key: str) -> None:
    """写入某个 provider 的 Key：去空白、空值拒绝、保留其他 provider 的原始条目。"""
    clean = key.strip() if isinstance(key, str) else ""
    if not clean:
        raise CredentialError(f"Key 不能为空，未写入（provider：{provider}）")
    data = read_auth(home)
    data[provider] = {"type": _CREDENTIAL_TYPE, "key": clean}
    write_auth(home, data)


def remove_entry(home: Path, provider: str) -> None:
    """删除某个 provider 的条目；条目或文件不存在时静默返回（幂等），不写空串占位。"""
    data = read_auth(home)
    if provider not in data:
        return
    del data[provider]
    write_auth(home, data)


def read_status(home: Path) -> dict[str, Any]:
    """读校验历史。缺失、非法 JSON、条目形状或时间戳不合法 → 一律 `{}`（降级为未校验）。"""
    path = status_path(home)
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    for entry in data.values():
        if not _valid_status_entry(entry):
            return {}
    return data


def write_status(home: Path, data: dict[str, Any]) -> None:
    """写校验历史（原子替换 + 权限收紧）。失败抛 `OSError`，由调用方降级显示。"""
    if not isinstance(data, dict):
        raise ValueError("校验历史必须是 JSON 对象")
    for entry in data.values():
        if not _valid_status_entry(entry):
            raise ValueError("校验历史条目形状不合法")
    path = status_path(home)
    ensure_home_dir(home)
    try:
        _atomic_write_json(path, data)
    except CredentialError as exc:
        raise OSError(f"写入校验历史失败：{path}") from exc


# ---------------------------------------------------------------- 形状校验


def _validate_entry(provider: str, entry: Any) -> None:
    """凭据条目必须是 `{"type": "api_key", "key": "<非空字符串>"}`。"""
    if not isinstance(entry, dict):
        raise CredentialError(f"凭据文件中 {provider} 条目必须是 JSON 对象")
    if entry.get("type") != _CREDENTIAL_TYPE:
        raise CredentialError(f"凭据文件中 {provider} 条目的 type 必须是 {_CREDENTIAL_TYPE}")
    key = entry.get("key")
    if not isinstance(key, str) or not key.strip():
        raise CredentialError(f"凭据文件中 {provider} 条目缺少有效的 key")


def _valid_status_entry(entry: Any) -> bool:
    """校验历史条目只含 `ok` / `checked_at` / `kind`，且值都合法。"""
    if not isinstance(entry, dict) or set(entry) != {"ok", "checked_at", "kind"}:
        return False
    if not isinstance(entry["ok"], bool):
        return False
    if entry["kind"] not in STATUS_KINDS:
        return False
    return _valid_checked_at(entry["checked_at"])


def _valid_checked_at(value: Any) -> bool:
    """时间戳必须是带时区偏移的 ISO-8601（规格要求精确到秒、带偏移）。"""
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.utcoffset() is not None


# ---------------------------------------------------------------- 原子写入


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """同目录临时文件 → flush/fsync → 收紧权限 → 重读校验 → 原子替换 → 替换后复验。

    任何失败都清理临时文件并向调用方抛 `CredentialError`；已被替换的情形只发生在
    最后一步只读复验上，此时不会改坏文件，但也不会谎称保存成功。
    """
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp_path: Path | None = None
    try:
        fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        tmp_path = Path(raw_tmp)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        windows_user = _harden_file(tmp_path)
        _verify_written(tmp_path, data)
        os.replace(tmp_path, path)
        tmp_path = None
        _verify_permissions(path, windows_user=windows_user)
    except CredentialError:
        _cleanup(tmp_path)
        raise
    except OSError as exc:
        _cleanup(tmp_path)
        raise CredentialError(f"写入凭据文件失败：{path}") from exc


def _verify_written(path: Path, data: dict[str, Any]) -> None:
    """把临时文件读回来比对，确认落盘内容就是准备写入的内容。"""
    try:
        written = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialError(f"凭据写入后重读失败：{path}") from exc
    if written != data:
        raise CredentialError(f"凭据写入后重读不一致：{path}")


def _cleanup(tmp_path: Path | None) -> None:
    if tmp_path is None:
        return
    try:
        tmp_path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- 权限


def _harden_file(path: Path) -> str:
    """收紧单个文件权限并校验。返回 Windows 当前用户（非 Windows 返回空串）。"""
    if _is_windows():
        return _lock_down_windows_acl(path)
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        raise CredentialError(f"无法收紧凭据文件权限：{path}") from exc
    _verify_posix_mode(path)
    return ""


def _verify_permissions(path: Path, *, windows_user: str | None = None) -> None:
    """只读复验权限（不修改），供替换后使用。"""
    if _is_windows():
        _verify_windows_acl(path, windows_user or _current_windows_user())
        return
    _verify_posix_mode(path)


def _verify_posix_mode(path: Path) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise CredentialError(f"无法读取凭据文件权限：{path}") from exc
    if mode != 0o600:
        raise CredentialError(f"凭据文件权限不是 0600（实际 {oct(mode)}）：{path}")


def _decode_output(raw: bytes | None) -> str:
    """按 UTF-8 → 系统 ANSI 代码页 → 替换式解码依次尝试。

    实测（Windows 11 + 中文用户名）：`whoami` 输出 UTF-8，`icacls` 输出 ANSI 代码页，
    所以不能固定用 `subprocess.run(text=True)`（会直接炸在多字节用户名上）。
    两次严格解码都失败时退回替换式解码：宁可校验不过而报错，也不伪造成功。
    """
    if not raw:
        return ""
    for encoding in ("utf-8", locale.getpreferredencoding(False)):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _run_cmd(args: list[str]) -> subprocess.CompletedProcess[str]:
    """调系统命令的唯一出口（测试在此打桩，不碰真实 icacls）。"""
    try:
        raw = subprocess.run(args, capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CredentialError(f"无法调用系统命令 {args[0]}：{exc}") from exc
    return subprocess.CompletedProcess(
        args, raw.returncode, _decode_output(raw.stdout), _decode_output(raw.stderr)
    )


def _current_windows_user() -> str:
    result = _run_cmd(["whoami"])
    user = (result.stdout or "").strip()
    if result.returncode != 0 or not user:
        raise CredentialError("无法确定当前 Windows 用户（whoami 失败），未保存凭据")
    return user


def _lock_down_windows_acl(path: Path) -> str:
    """关继承 + 只给当前用户与 SYSTEM 完全控制，然后读回校验。

    `os.chmod()` 在 Windows 上只影响只读属性，凭据隔离必须以 NTFS ACL 为准
    （规格 002 第 8 章）；icacls 不可用或校验不过一律报错，不谎称保存成功。
    """
    user = _current_windows_user()
    target = str(path)
    for args in (
        ["icacls", target, "/inheritance:r"],
        ["icacls", target, "/grant:r", f"{user}:F"],
        ["icacls", target, "/grant:r", "SYSTEM:F"],
    ):
        result = _run_cmd(args)
        if result.returncode != 0:
            raise CredentialError(f"收紧凭据文件权限失败（icacls 返回 {result.returncode}）：{path}")
    _verify_windows_acl(path, user)
    return user


def _verify_windows_acl(path: Path, user: str) -> None:
    result = _run_cmd(["icacls", str(path)])
    if result.returncode != 0:
        raise CredentialError(f"无法读取凭据文件 ACL（icacls 返回 {result.returncode}）：{path}")
    text = (result.stdout or "").lower()
    if user.lower() not in text:
        raise CredentialError(f"凭据文件 ACL 未包含当前用户，未保存凭据：{path}")
    for subject in _BROAD_ACL_SUBJECTS:
        if subject in text:
            raise CredentialError(f"凭据文件 ACL 仍含宽泛主体 {subject}，未保存凭据：{path}")
