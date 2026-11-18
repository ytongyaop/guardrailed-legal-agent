"""统一错误体（规格 006 §2「错误约定」）。

非 2xx 一律 `{error:{code,message,details,action},request_id}`。本模块是**唯一**构造错误
响应的地方：路由只抛 `ApiError`，框架层的 `RequestValidationError`、`HTTPException` 与
未捕获异常在这里映射成同一形状，避免各路由各写一套。

口径：
- 状态码与 `code` 一一对应（见下表常量），不新造码；
- `details` / `action` 显式 `null`，不让前端判断字段是否存在；
- 未捕获异常的 message 用固定文案，**不回显异常文本**（可能含路径、SQL、凭据片段）。
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# ---------------------------------------------------------------- 状态码 ↔ code
VALIDATION_ERROR = "VALIDATION_ERROR"  # 400 字段错误 / 业务校验
UNAUTHENTICATED = "UNAUTHENTICATED"  # 401
FORBIDDEN = "FORBIDDEN"  # 403 已登录但无权限
CSRF_FAILED = "CSRF_FAILED"  # 403 缺 token / token 不符 / 跨站来源
NOT_FOUND = "NOT_FOUND"  # 404 含「越权访问他人企业资源」
STATE_CONFLICT = "STATE_CONFLICT"  # 409
NO_MODEL = "NO_MODEL"  # 409 无模型门禁（批次 3 使用）
PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"  # 413
UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"  # 415
FILE_PARSE_ERROR = "FILE_PARSE_ERROR"  # 422
MODEL_KEY_INVALID = "MODEL_KEY_INVALID"  # 422
MODEL_PROBE_FAILED = "MODEL_PROBE_FAILED"  # 502
INTERNAL_ERROR = "INTERNAL_ERROR"  # 500

ActionType = Literal["open_settings", "contact_admin", "retry", "none"]


def make_action(kind: ActionType, label: str) -> dict[str, str]:
    """构造 `action`：前端据此渲染一个可点的下一步，而不是自己猜。"""
    return {"type": kind, "label": label}


class ApiError(Exception):
    """路由层唯一的业务异常；由 `install_error_handlers` 统一转成错误体。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        action: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = code
        self.message = message
        self.details = details
        self.action = action


def new_request_id() -> str:
    """请求号：只用于关联日志与前端报错，不含任何用户信息。"""
    return "req_" + uuid.uuid4().hex[:16]


def current_request_id(request: Request) -> str:
    """取中间件写进 `request.state` 的请求号；缺失（异常路径）时返回空串而不是抛错。"""
    value = getattr(request.state, "request_id", "")
    return value if isinstance(value, str) else ""


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    action: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details,
                "action": action,
            },
            "request_id": current_request_id(request),
        },
    )


# 框架层状态码 → 本表 code。405 收敛成 404：既不暴露"路径存在但方法不对"，
# 也让错误码表保持 006 里那几种，不额外发明 METHOD_NOT_ALLOWED。
_STATUS_CODES: dict[int, str] = {
    400: VALIDATION_ERROR,
    401: UNAUTHENTICATED,
    403: FORBIDDEN,
    404: NOT_FOUND,
    405: NOT_FOUND,
    413: PAYLOAD_TOO_LARGE,
    415: UNSUPPORTED_FILE_TYPE,
    422: VALIDATION_ERROR,
}


def install_error_handlers(app: FastAPI) -> None:
    """挂上四个处理器：业务异常、请求校验、框架 HTTP 异常、兜底 500。"""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(
            request,
            exc.status_code,
            exc.code,
            exc.message,
            details=exc.details,
            action=exc.action,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 只回字段路径与原因，不回显原始输入：请求体里可能有口令或 Key。
        fields = sorted(
            ".".join(str(part) for part in err.get("loc", ()) if part not in ("body", "query", "path"))
            for err in exc.errors()
        )
        return error_response(
            request,
            400,
            VALIDATION_ERROR,
            "请求参数不合法，请检查后重试",
            details={"fields": [f for f in fields if f]},
            action=make_action("none", "修改后重试"),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        status = 404 if exc.status_code == 405 else exc.status_code
        code = _STATUS_CODES.get(status, INTERNAL_ERROR)
        if code == INTERNAL_ERROR:
            return error_response(
                request,
                500,
                INTERNAL_ERROR,
                "服务器内部错误，请稍后重试",
                action=make_action("retry", "重试"),
            )
        message = "请求的资源不存在" if code == NOT_FOUND else str(exc.detail or "请求无法处理")
        return error_response(request, status, code, message)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        return error_response(
            request,
            500,
            INTERNAL_ERROR,
            "服务器内部错误，请稍后重试",
            action=make_action("retry", "重试"),
        )
