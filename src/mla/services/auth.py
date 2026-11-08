"""账号与权限：密码只存 pbkdf2 哈希 + 随机盐，不存明文、不写日志。

角色：用户（只能看自己的企业/合同/任务）、管理员（可管用户、看法源与规则、看全量日志）。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid

from ..domain.models import Enterprise, Role, User, UserDeletion
from ..ports.repository import Repository

_ITER = 120_000

# 口令下限：注册、首次初始化、改口令统一用这一处，避免三条路径各说各话
MIN_PASSWORD_LEN = 8


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """返回 (hash_hex, salt_hex)。同一密码每次盐不同。"""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _ITER)
    return dk.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    """常量时间比较，避免时序侧信道。"""
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _ITER)
    return secrets.compare_digest(dk.hex(), password_hash)


def register(
    repo: Repository, username: str, password: str, role: Role = Role.USER, enterprise_id: str = ""
) -> User:
    """注册；用户名重复直接报错（不静默覆盖）。"""
    username = username.strip()
    if len(username) < 3:
        raise ValueError("用户名至少 3 个字符")
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"密码至少 {MIN_PASSWORD_LEN} 位")
    if repo.get_user_by_name(username):
        raise ValueError(f"用户名已存在：{username}")
    digest, salt = hash_password(password)
    user = User(
        user_id="U-" + uuid.uuid4().hex[:10],
        username=username,
        password_hash=digest,
        salt=salt,
        role=role,
        enterprise_id=enterprise_id,
    )
    repo.create_user(user)
    return user


def authenticate(repo: Repository, username: str, password: str) -> User | None:
    """登录：用户不存在与密码错误返回同样的 None（不泄露账号是否存在）。"""
    user = repo.get_user_by_name(username.strip())
    if not user:
        return None
    return user if verify_password(password, user.password_hash, user.salt) else None


def is_admin(user: User) -> bool:
    return user.role is Role.ADMIN


def require_admin(user: User | None) -> None:
    """管理端页面的门禁：不是管理员就抛异常（UI 层捕获后提示无权限）。"""
    if user is None:
        raise PermissionError("未登录")
    if not is_admin(user):
        raise PermissionError("需要管理员权限")


def has_admin(repo: Repository) -> bool:
    """库里是否已有管理员：界面据此决定显示"首次初始化"还是"登录"。"""
    return any(is_admin(u) for u in repo.list_users())


def change_password(repo: Repository, user: User, old_password: str, new_password: str) -> tuple[bool, str]:
    """改口令：先验旧口令，再校验新口令长度与是否与原口令相同。

    返回 (成功?, 失败原因)；成功时原因是空串。密码只存哈希 + 新盐，不写日志。
    """
    if not verify_password(old_password, user.password_hash, user.salt):
        return False, "当前口令不正确"
    if len(new_password) < MIN_PASSWORD_LEN:
        return False, f"新口令至少 {MIN_PASSWORD_LEN} 位"
    if new_password == old_password:
        return False, "新口令不能与当前口令相同"
    digest, salt = hash_password(new_password)
    repo.set_user_password(user.user_id, digest, salt)
    return True, ""


def create_first_admin(repo: Repository, username: str, password: str, enterprise_name: str = "") -> User:
    """首次初始化：建管理员账号；给了企业名就同时建企业并绑定。

    口令不足 8 位直接抛 ValueError（界面会先拦一道）。
    先查用户名再建企业，避免中途报错留下孤零零的企业档案。
    """
    username = username.strip()
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"管理员口令至少 {MIN_PASSWORD_LEN} 位")
    if repo.get_user_by_name(username):
        raise ValueError(f"用户名已存在：{username}")
    enterprise_id = ""
    if enterprise_name.strip():
        enterprise_id = "E-" + uuid.uuid4().hex[:8]
        repo.save_enterprise(Enterprise(enterprise_id=enterprise_id, name=enterprise_name.strip()))
    return register(repo, username, password, Role.ADMIN, enterprise_id)


def delete_account(repo: Repository, actor: User, target_username: str) -> UserDeletion:
    """管理员硬删账号：级联该账号的企业/合同/任务/日志，并留一条管理员日志。

    保护顺序固定（非管理员直接抛错，其余以 `deleted=False + reason` 返回）：
    账号不存在 → 删自己 → 最后一个管理员 → 企业仍被其他账号绑定。
    """
    if not is_admin(actor):
        raise PermissionError("需要管理员权限")
    target = repo.get_user_by_name(target_username.strip())
    if target is None:
        return UserDeletion(deleted=False, reason="账号不存在")
    if target.user_id == actor.user_id:
        return UserDeletion(deleted=False, reason="不能删除当前登录的账号")
    if is_admin(target):
        if sum(1 for u in repo.list_users() if is_admin(u)) <= 1:
            return UserDeletion(deleted=False, reason="系统至少保留一个管理员")
    if target.enterprise_id:
        bound_by_others = any(
            u.enterprise_id == target.enterprise_id and u.user_id != target.user_id for u in repo.list_users()
        )
        if bound_by_others:
            return UserDeletion(deleted=False, reason="该企业仍被其他账号绑定，请先解绑")
    return repo.delete_user(target.user_id, actor_id=actor.user_id, log_id="L-" + uuid.uuid4().hex[:10])
