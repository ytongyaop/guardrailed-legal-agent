"""合同档案（规格 006 §2）：列表 / 解析 / 读取 / 删除。

- 一律按**当前账号的企业**读写：跨企业的一律 404（不是 403，避免探测他人合同是否存在）；
- `POST /contracts/parse` 只解析不存档（存档在核对成功后由批次 3 的链路负责），
  接受 multipart `file`（txt / docx / 文本型 pdf，上限 200 MiB）或 JSON `{filename,text}`；
- 文件名统一走 `normalize_contract_name`，与旧界面同一口径。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from ...adapters.files import normalize_contract_name, parse_contract_bytes
from ..deps import AuthDep, RepoDep, require_capability
from ..errors import (
    FILE_PARSE_ERROR,
    NOT_FOUND,
    PAYLOAD_TOO_LARGE,
    UNSUPPORTED_FILE_TYPE,
    VALIDATION_ERROR,
    ApiError,
)
from ..schemas import ContractDetail, ContractPage, ContractSummary, ParseResult, ParseTextIn

router = APIRouter()

#: 上传上限：200 MiB（006 §2「multipart file（上限 200 MiB）」）
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
ALLOWED_SUFFIXES = (".txt", ".docx", ".pdf")
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


@router.get(
    "/contracts",
    dependencies=[Depends(require_capability("contracts"))],
    response_model=ContractPage,
)
def list_contracts(
    auth: AuthDep,
    repo: RepoDep,
    q: str = "",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> ContractPage:
    """本企业合同列表：`q` 同时匹配文件名与正文（与旧界面一致）。

    `total` 是筛选后的条数，`total_all` 是本企业全部条数——页头要能说清"共 N 份 · 筛出 M 份"。
    """
    enterprise_id = auth.user.enterprise_id
    all_contracts = repo.list_contracts(enterprise_id) if enterprise_id else []
    keyword = q.strip()
    rows = [
        rec
        for rec in all_contracts
        if not keyword
        or keyword in rec.filename
        or keyword in normalize_contract_name(rec.filename)
        or keyword in rec.text
    ]
    size = min(max(int(page_size), 1), MAX_PAGE_SIZE)
    current = max(int(page), 1)
    start = (current - 1) * size
    return ContractPage(
        items=[ContractSummary.of(rec) for rec in rows[start : start + size]],
        total=len(rows),
        page=current,
        page_size=size,
        total_all=len(all_contracts),
    )


@router.post(
    "/contracts/parse",
    dependencies=[Depends(require_capability("contract_parse"))],
    response_model=ParseResult,
)
async def parse_contract(request: Request) -> ParseResult:
    """只解析不存档：multipart 上传或 JSON 文本二选一。"""
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith("multipart/form-data"):
        filename, data = await _read_upload(request)
        return _parse_bytes(filename, data)
    if content_type.startswith("application/json"):
        return _parse_json(await _read_limited_body(request, MAX_UPLOAD_BYTES))
    raise ApiError(
        415,
        UNSUPPORTED_FILE_TYPE,
        "请用 multipart 上传文件，或用 JSON 提交 {filename, text}",
    )


@router.get(
    "/contracts/{contract_id}",
    dependencies=[Depends(require_capability("contracts"))],
    response_model=ContractDetail,
)
def get_contract(contract_id: str, auth: AuthDep, repo: RepoDep) -> ContractDetail:
    rec = repo.get_contract(contract_id, auth.user.enterprise_id)
    if rec is None:
        raise ApiError(404, NOT_FOUND, "合同不存在")
    return ContractDetail.of(rec)


@router.delete(
    "/contracts/{contract_id}",
    status_code=204,
    dependencies=[Depends(require_capability("contracts"))],
)
def delete_contract(contract_id: str, auth: AuthDep, repo: RepoDep) -> None:
    """硬删本企业合同；他企业的同一 ID 也是 404（仓储层按企业过滤）。"""
    if repo.get_contract(contract_id, auth.user.enterprise_id) is None:
        raise ApiError(404, NOT_FOUND, "合同不存在")
    repo.delete_contract(contract_id, auth.user.enterprise_id)


# ---------------------------------------------------------------- 内部


def _parse_bytes(filename: str, data: bytes) -> ParseResult:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ApiError(
            415,
            UNSUPPORTED_FILE_TYPE,
            "只支持 .txt / .docx / 文本型 .pdf",
            details={"filename": normalize_contract_name(filename)},
        )
    try:
        text = parse_contract_bytes(filename, data)
    except ImportError as exc:
        raise ApiError(
            422,
            FILE_PARSE_ERROR,
            "解析该类型需要可选依赖（python-docx / pypdf），请改用 .txt 或粘贴文本",
        ) from exc
    except Exception as exc:  # 损坏文件由适配器抛出各类异常，统一成 422
        raise ApiError(422, FILE_PARSE_ERROR, "文件解析失败，请改用粘贴文本") from exc
    return ParseResult(filename=normalize_contract_name(filename), text=text, char_count=len(text))


def _parse_json(body: bytes) -> ParseResult:
    if not body:
        raise ApiError(400, VALIDATION_ERROR, "请求体为空，需要 {filename, text}")
    try:
        payload = ParseTextIn.model_validate_json(body)
    except ValidationError as exc:
        raise ApiError(
            400,
            VALIDATION_ERROR,
            "请求体需要 {filename, text}",
            details={"fields": [str(part) for err in exc.errors() for part in err.get("loc", ())]},
        ) from exc
    if not payload.filename.strip():
        raise ApiError(400, VALIDATION_ERROR, "filename 不能为空", details={"fields": ["filename"]})
    return ParseResult(
        filename=normalize_contract_name(payload.filename),
        text=payload.text,
        char_count=len(payload.text),
    )


def _reject_declared_oversize(request: Request, limit: int) -> None:
    """先看声明的长度：正常浏览器与 httpx 都会带 Content-Length，能在读之前就拒绝。"""
    declared = request.headers.get("content-length") or ""
    if declared.isdigit() and int(declared) > limit:
        raise ApiError(413, PAYLOAD_TOO_LARGE, f"上传内容超过上限 {limit // (1024 * 1024)} MiB")


async def _read_limited_body(request: Request, limit: int) -> bytes:
    _reject_declared_oversize(request, limit)
    body = await request.body()
    if len(body) > limit:
        raise ApiError(413, PAYLOAD_TOO_LARGE, f"上传内容超过上限 {limit // (1024 * 1024)} MiB")
    return body


async def _read_upload(request: Request) -> tuple[str, bytes]:
    limit = MAX_UPLOAD_BYTES
    _reject_declared_oversize(request, limit)
    form = await request.form(max_files=1, max_fields=2)
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise ApiError(
            400,
            VALIDATION_ERROR,
            "缺少文件字段 file",
            details={"fields": ["file"]},
        )
    filename = upload.filename or ""
    try:
        data = await upload.read(limit + 1)
    finally:
        await upload.close()
    if len(data) > limit:
        raise ApiError(413, PAYLOAD_TOO_LARGE, f"上传文件超过上限 {limit // (1024 * 1024)} MiB")
    return filename, data
