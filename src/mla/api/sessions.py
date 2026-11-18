"""服务端内存会话（规格 006 §4）。

- 会话 ID 是 `secrets.token_urlsafe(32)` 生成的 256 位不透明随机串，不用 JWT，也不需要
  持久签名密钥；
- CSRF token 与会话 ID **各自独立随机**，登录/注册/初始化成功时一起轮换；
- 30 分钟空闲过期、12 小时绝对过期，进程重启即全部失效；
- 每个请求按 `user_id` 重读用户，并比对口令指纹：改密、改角色、删号立即生效；
- 只存内存，不写 `settings` 表、不落盘。

这里刻意不放任何 `user` 快照：会话只记 `user_id` + 口令指纹，权限一律每次重读。
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..domain.models import User

COOKIE_NAME = "mla_session"
DEFAULT_IDLE = timedelta(minutes=30)
DEFAULT_ABSOLUTE = timedelta(hours=12)


@dataclass(frozen=True)
class SessionConfig:
    """会话参数。测试用它把空闲/绝对过期缩短，避免真实等待。"""

    idle: timedelta = DEFAULT_IDLE
    absolute: timedelta = DEFAULT_ABSOLUTE
    cookie_name: str = COOKIE_NAME
    # 本地 HTTP 默认不加 Secure；只有显式 MLA_COOKIE_SECURE=1 且确实走 HTTPS 时才开。
    secure: bool = False


def credential_fingerprint(user: User) -> str:
    """口令指纹：哈希或盐一变（改密）旧会话立刻对不上，不需要额外的失效表。"""
    raw = f"{user.password_hash}\x00{user.salt}".encode()
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    user_id: str
    csrf_token: str
    fingerprint: str
    created_at: datetime
    last_seen: datetime


class SessionStore:
    """进程内会话表。单进程单 worker 才有意义（006 §1 明确不做多 worker）。"""

    def __init__(self, clock: Callable[[], datetime], config: SessionConfig | None = None) -> None:
        self._clock = clock
        self._config = config or SessionConfig()
        self._sessions: dict[str, SessionRecord] = {}
        self._lock = threading.Lock()

    @property
    def config(self) -> SessionConfig:
        return self._config

    def create(self, user: User) -> SessionRecord:
        """新建会话：会话 ID 与 CSRF token 都是独立随机串。"""
        now = self._clock()
        record = SessionRecord(
            session_id=secrets.token_urlsafe(32),
            user_id=user.user_id,
            csrf_token=secrets.token_urlsafe(32),
            fingerprint=credential_fingerprint(user),
            created_at=now,
            last_seen=now,
        )
        with self._lock:
            self._sessions[record.session_id] = record
        return record

    def lookup(self, session_id: str) -> SessionRecord | None:
        """取会话并顺带续期；过期（空闲或绝对）当场删除，返回 None。"""
        if not session_id:
            return None
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return None
            now = self._clock()
            if self._expired(record, now):
                del self._sessions[session_id]
                return None
            refreshed = SessionRecord(
                session_id=record.session_id,
                user_id=record.user_id,
                csrf_token=record.csrf_token,
                fingerprint=record.fingerprint,
                created_at=record.created_at,
                last_seen=now,
            )
            self._sessions[session_id] = refreshed
            return refreshed

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def delete_other_sessions(self, user_id: str, keep: str) -> int:
        """改密后清掉该账号的其他会话，保留当前会话（当前会话换新指纹）。"""
        with self._lock:
            stale = [
                sid for sid, record in self._sessions.items() if record.user_id == user_id and sid != keep
            ]
            for sid in stale:
                del self._sessions[sid]
            return len(stale)

    def rebind(self, session: SessionRecord, user: User) -> SessionRecord:
        """改密后用新口令指纹刷新同一会话，避免用户刚改完密码就被自己踢下线。"""
        refreshed = SessionRecord(
            session_id=session.session_id,
            user_id=session.user_id,
            csrf_token=session.csrf_token,
            fingerprint=credential_fingerprint(user),
            created_at=session.created_at,
            last_seen=session.last_seen,
        )
        with self._lock:
            self._sessions[session.session_id] = refreshed
        return refreshed

    def count(self) -> int:
        """当前会话条数（只用于测试与诊断，不含任何用户信息）。"""
        with self._lock:
            return len(self._sessions)

    def _expired(self, record: SessionRecord, now: datetime) -> bool:
        if now - record.last_seen > self._config.idle:
            return True
        return now - record.created_at > self._config.absolute
