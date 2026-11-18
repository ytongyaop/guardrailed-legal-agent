"""Word 报告下载（规格 006 §2：`GET /tasks/{task_id}/report`）。

直接调用 `services.report.build_report_docx`，**不重写模板、不二次排版**：报告里的分项计数、
条目顺序与免责声明必须与旧界面下载到的字节级内容同源（批 3 parity 测试逐行比对快照文本）。
文件名与 MIME 沿用旧界面：`核对报告-{task_id}.docx`。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from ...services.report import build_report_docx
from ..deps import AuthDep, RepoDep, require_capability
from .checks import resolve_readable_task

router = APIRouter()

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@router.get(
    "/tasks/{task_id}/report",
    dependencies=[Depends(require_capability("report"))],
    response_class=Response,
)
def get_report(task_id: str, auth: AuthDep, repo: RepoDep, request: Request) -> Response:
    """下载报告：当前内存任务或本企业历史任务；越权与不存在一律 404。"""
    task, _history, _clause_review = resolve_readable_task(request, repo, task_id, auth.user)
    blob = build_report_docx(task, repo)
    return Response(
        content=blob,
        media_type=DOCX_MIME,
        headers={"Content-Disposition": _attachment(f"核对报告-{task.task_id}.docx")},
    )


def _attachment(filename: str) -> str:
    """同时给 ASCII 兜底名与 RFC 5987 的 UTF-8 名：中文名在旧浏览器上也不乱码。"""
    quoted = quote(filename, safe="")
    return f"attachment; filename=\"report.docx\"; filename*=UTF-8''{quoted}"
