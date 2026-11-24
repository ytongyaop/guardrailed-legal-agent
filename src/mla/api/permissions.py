"""能力矩阵（规格 006 §2 端点表）：鉴权由这张表驱动，不由路由各自记规则。

表与 `tests/api/fixtures/permissions.json`（批次 1 冻结）一一对应：
- `anonymous`：公开端点，不要求登录，也不校验 CSRF（写操作只校验同源 Origin）；
- `admin`：要求登录 + 管理员角色；普通用户访问一律 403 `FORBIDDEN`，不是 404
  （管理端入口的存在性不是秘密，隐藏的是数据）；
- 其余：要求登录；写操作额外校验 `X-CSRF-Token`。

`tests/api/test_permissions.py` 会同时核对「本表 == 冻结夹具」和「本表 == 实际注册的路由」，
所以新增端点却忘了授权、或授权了却不存在的端点，都会在测试里立刻暴露。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    """一项能力：端点清单 + 访问门槛。`endpoints` 里的方法/路径不含 `/api` 前缀。"""

    name: str
    anonymous: bool
    admin: bool
    endpoints: tuple[tuple[str, str], ...]


def _cap(
    name: str,
    endpoints: tuple[tuple[str, str], ...],
    *,
    anonymous: bool = False,
    admin: bool = False,
) -> Capability:
    return Capability(name=name, anonymous=anonymous, admin=admin, endpoints=endpoints)


CAPABILITIES: dict[str, Capability] = {
    # ---- 公开（无会话）：只做同源 Origin 校验，不校验 CSRF token
    "setup": _cap("setup", (("GET", "/setup"), ("GET", "/setup/sample-contract")), anonymous=True),
    "setup_admin": _cap("setup_admin", (("POST", "/setup/admin"),), anonymous=True),
    "login": _cap("login", (("POST", "/auth/login"),), anonymous=True),
    "register": _cap("register", (("POST", "/auth/register"),), anonymous=True),
    # ---- 登录态（所有角色）
    "auth_me": _cap("auth_me", (("GET", "/auth/me"),)),
    "logout": _cap("logout", (("POST", "/auth/logout"),)),
    "onboarding": _cap("onboarding", (("POST", "/auth/onboarding"),)),
    "settings_password": _cap("settings_password", (("PUT", "/auth/password"),)),
    "enterprise": _cap(
        "enterprise",
        (("GET", "/enterprise"), ("PUT", "/enterprise"), ("DELETE", "/enterprise")),
    ),
    "contracts": _cap(
        "contracts",
        (
            ("GET", "/contracts"),
            ("GET", "/contracts/{contract_id}"),
            ("DELETE", "/contracts/{contract_id}"),
        ),
    ),
    "contract_parse": _cap("contract_parse", (("POST", "/contracts/parse"),)),
    # 核对链路：挂载在 `routes/checks.py`（异步 job）与 `routes/history.py`、`routes/report.py`
    "check": _cap(
        "check",
        (
            ("POST", "/checks"),
            ("GET", "/jobs/{job_id}"),
            ("GET", "/tasks/{task_id}"),
            ("POST", "/tasks/{task_id}/answers"),
            ("POST", "/tasks/{task_id}/optional-facts"),
        ),
    ),
    "qa": _cap("qa", (("POST", "/tasks/{task_id}/qa"),)),
    "history": _cap("history", (("GET", "/history"),)),
    "history_trend": _cap("history_trend", (("GET", "/history/trend"),)),
    "history_delete": _cap("history_delete", (("DELETE", "/history/{task_id}"),)),
    "report": _cap("report", (("GET", "/tasks/{task_id}/report"),)),
    "settings_ui": _cap("settings_ui", (("GET", "/settings/ui"), ("PUT", "/settings/ui"))),
    # ---- 管理员
    "settings_model": _cap(
        "settings_model",
        (
            ("GET", "/settings/model"),
            ("PUT", "/settings/model"),
            ("POST", "/settings/model/{provider}/test"),
            ("DELETE", "/settings/model/{provider}"),
            ("PUT", "/settings/embedding"),
            ("DELETE", "/settings/embedding"),
        ),
        admin=True,
    ),
    "admin_users": _cap(
        "admin_users",
        (("GET", "/admin/users"), ("DELETE", "/admin/users/{user_id}")),
        admin=True,
    ),
    "admin_role": _cap("admin_role", (("PATCH", "/admin/users/{user_id}/role"),), admin=True),
    "admin_statutes": _cap("admin_statutes", (("GET", "/admin/statutes"),), admin=True),
    "admin_rules": _cap("admin_rules", (("GET", "/admin/rules"),), admin=True),
    "admin_logs": _cap(
        "admin_logs",
        (("GET", "/admin/logs"), ("GET", "/admin/logs/export")),
        admin=True,
    ),
    "admin_evaluation": _cap("admin_evaluation", (("GET", "/admin/evaluation"),), admin=True),
}


def capability(name: str) -> Capability:
    """按名字取能力；名字写错立刻报错（启动期就发现，而不是运行时静默放行）。"""
    try:
        return CAPABILITIES[name]
    except KeyError as exc:
        raise KeyError(f"未定义的能力：{name}") from exc
