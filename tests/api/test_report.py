"""Word 报告下载（规格 006 AC-05 的 API 部分）。

三条断言：

1. **不重写模板**：经 API 拿到的 docx 与本地对同一任务调 `build_report_docx` 的正文逐行相同；
2. MIME 与文件名沿用旧界面（`核对报告-{task_id}.docx`，中文名用 RFC 5987 编码）；
3. 访问边界与详情一致：未登录 401、越权/不存在/已删 404。

与 v0.3.2 冻结文本的逐行比对在 `test_parity.py`（四个夹具全覆盖）。
"""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

import pytest
from docx import Document

from mla.adapters.persist import SqliteRepository
from mla.domain.models import (
    CheckItem,
    CheckTask,
    ItemKind,
    Severity,
    Suggestion,
    TaskMode,
)
from mla.services.report import build_report_docx

from .api_helpers import csrf_of, register, setup_admin
from .flow_helpers import seed_task

pytestmark = pytest.mark.no_llm

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _task(task_id: str = "T-REPORT") -> CheckTask:
    return CheckTask(
        task_id=task_id,
        mode=TaskMode.CHECK,
        contract_name="报告演示.txt",
        contract_chars=1200,
        clause_review=True,
        created_at=datetime(2026, 1, 5, 9, 0, 0),
        items=[
            CheckItem(
                item_id="I-1",
                kind=ItemKind.RISK,
                clause_ref="第十九条",
                finding="试用期约定超过法定上限",
                source_id="LCL-19",
                severity=Severity.HARD,
                rule_id="R-PROBATION-6M",
            )
        ],
        suggestions=[Suggestion(text="把试用期调整到法定上限内。", source_ids=("LCL-19",))],
    )


def _docx_text(blob: bytes) -> str:
    document = Document(io.BytesIO(blob))
    lines = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            lines.append("\t".join(cell.text for cell in row.cells))
    return "\n".join(lines)


@pytest.fixture
def seeded(api, tmp_path: Path):
    setup_admin(api.client)
    enterprise = api.client.get("/api/enterprise").json()
    assert enterprise is not None
    task = _task()
    task.enterprise_id = enterprise["enterprise_id"]
    seed_task(api.db_path, task)
    repo = SqliteRepository(db_path=str(tmp_path / "local.sqlite3"))
    return api, task, repo, enterprise["enterprise_id"]


def test_report_matches_local_build(seeded) -> None:
    """API 下载的字节流的正文 == 本地 `build_report_docx` 的正文（同一模板、同一数据）。"""
    api, task, repo, _enterprise_id = seeded
    try:
        response = api.client.get(f"/api/tasks/{task.task_id}/report")
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].split(";")[0] == DOCX_MIME
        assert response.content[:2] == b"PK"

        disposition = response.headers["content-disposition"]
        assert disposition.startswith("attachment;")
        assert "filename*=UTF-8''" in disposition
        assert "%E6%A0%B8%E5%AF%B9%E6%8A%A5%E5%91%8A-" in disposition, "中文文件名按 UTF-8 百分号编码"
        assert task.task_id in disposing_decoded(disposition)

        local = _docx_text(build_report_docx(task, repo))
        remote = _docx_text(response.content)
        assert remote == local
        assert "发现 1 条与法条存在冲突的事项" in remote
        assert "处理建议" in remote
        assert "不构成法律意见" in remote
    finally:
        repo.close()


def disposing_decoded(disposition: str) -> str:
    """把 `filename*=UTF-8''...` 解回来，断言文件名本身（而不是编码后的字节）。"""
    from urllib.parse import unquote

    _, _, tail = disposition.partition("filename*=UTF-8''")
    return unquote(tail.strip())


def test_report_requires_login(api) -> None:
    assert api.client.get("/api/tasks/T-X/report").status_code == 401


def test_report_not_found(seeded) -> None:
    api, _task_obj, repo, _enterprise_id = seeded
    try:
        missing = api.client.get("/api/tasks/T-MISSING/report")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "NOT_FOUND"
    finally:
        repo.close()


def test_report_is_enterprise_scoped(seeded) -> None:
    """他人企业的任务报告也是 404（不暴露存在性）。"""
    api, task, repo, _enterprise_id = seeded
    try:
        other = register(api.client, username="outsider2", enterprise_name="另一家企业")
        assert csrf_of(other)
        response = api.client.get(f"/api/tasks/{task.task_id}/report")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"
    finally:
        repo.close()
