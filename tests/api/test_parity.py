"""结果 parity（规格 006 批次 3 / AC-03、AC-05）：API 输出必须与批次 1 冻结的快照逐字段一致。

为什么用批次 1 的四个夹具而不是另造一份：快照冻的是 **v0.3.2 的领域输出**（`summarize_verdict` /
`sort_items` / `merge_findings` / `build_report_docx`）。这里把同一份任务重放一次、落库、再走
API 取详情与报告，等于用同一把尺子同时量旧渲染与新接口——任何"前端补算"或"presenter 重排"
都会立刻对不上。

四个夹具各自守什么（与 `test_legacy_snapshot.py` 同一张表）：

| 夹具 | 守的东西 |
| --- | --- |
| `model_first_rich` | 模型主导 + 程序数值复核条目，跨 高/中/低 与 确定冲突/待确认 |
| `model_first_empty` | 零条目但不是 clean（覆盖不完整），结论字段不能凭空造数 |
| `legacy_path` | 旧路径：`merge_findings` 的「规则 → A → B → C」顺序 |
| `mixed_grades` | 同一影响度内混合确定性（四键排序） |
"""

from __future__ import annotations

import io
import json
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from docx import Document

HERE = Path(__file__).resolve().parent
FIXTURES_DIR = HERE / "fixtures"
TESTS_DIR = HERE.parent
for _path in (str(FIXTURES_DIR), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from build_tasks import (  # noqa: E402  # pyright: ignore[reportMissingImports]
    DATE_PLACEHOLDER,
    FIXTURES,
    TASK_ID_PLACEHOLDER,
    FixtureSpec,
    build_task,
    make_repo,
    normalize_ws,
)

from mla.api import presenters  # noqa: E402

from .api_helpers import csrf_of, setup_admin  # noqa: E402
from .flow_helpers import seed_task  # noqa: E402

pytestmark = pytest.mark.mock_llm

DATE_ROW = re.compile(r"^生成日期\t.*$", re.MULTILINE)
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
ROW_FIELDS = ("item_id", "finding", "impact", "certainty")


def _expected(spec: FixtureSpec) -> dict[str, Any]:
    path = FIXTURES_DIR / f"{spec.name}.snapshot.json"
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def _report_text(blob: bytes, task_id: str) -> str:
    """把下载到的 docx 转成与 `*.report.txt` 同构的文本（段落 + 表格，抹掉随机值）。"""
    document = Document(io.BytesIO(blob))
    lines = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            lines.append("\t".join(cell.text for cell in row.cells))
    text = "\n".join(lines).replace(task_id, TASK_ID_PLACEHOLDER)
    return DATE_ROW.sub(f"生成日期\t{DATE_PLACEHOLDER}", text)


@pytest.fixture
def replayed(api, monkeypatch, tmp_path):
    """把每个夹具重放一次并落库到 API 的库里；返回（夹具 → (原任务, 落库任务)）的工厂。"""
    state = setup_admin(api.client)
    enterprise = api.client.get("/api/enterprise").json()
    assert enterprise is not None
    enterprise_id = enterprise["enterprise_id"]

    def build(spec: FixtureSpec):
        # 重放用独立库：不碰 API 库里的用户/任务，避免把夹具的 U-fixed 带进来
        repo = make_repo(str(tmp_path / f"{spec.name}-replay.sqlite3"))
        task = build_task(spec, patch=monkeypatch, repo=repo)
        stored = seed_task(api.db_path, replace(task, enterprise_id=enterprise_id))
        return task, stored

    return api, state, build


def _get_detail(client, task_id: str) -> dict[str, Any]:
    response = client.get(f"/api/tasks/{task_id}")
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


@pytest.mark.parametrize("spec", FIXTURES, ids=[spec.name for spec in FIXTURES])
def test_task_detail_matches_snapshot(spec: FixtureSpec, replayed) -> None:
    """条目与结论：id / 正文 / 影响度 / 确定性逐字段相等，顺序完全相同。"""
    api, _state, build = replayed
    _task, stored = build(spec)
    expected = _expected(spec)
    detail = _get_detail(api.client, stored.task_id)

    verdict = detail["verdict"]
    for key in ("status", "headline", "risk_count", "review_count"):
        assert verdict[key] == expected["verdict"][key], f"{spec.name}: verdict.{key} 漂移"

    rows = detail["findings"]
    if expected["task"]["model_first"]:
        assert {row["path"] for row in rows} <= {"consensus"}
        got = [tuple(row[field] for field in ROW_FIELDS) for row in rows]
        want = [tuple(row[field] for field in ROW_FIELDS) for row in expected["sorted_items"]]
        assert got == want, f"{spec.name}: 模型主导路径的条目/顺序漂移"
    else:
        # 旧路径的展示顺序来自 `merge_findings`：规则条目按 rule_id，条款结论按（段落, 触发器）
        merged = expected["merge_findings"]
        by_id = {row["item_id"]: row for row in expected["sorted_items"]}
        rule_rows = [row for row in merged if row["kind"] == "item"]
        clause_rows = [row for row in merged if row["kind"] == "clause_finding"]
        got_rules = [row for row in rows if row["path"] == "rule"]
        got_clauses = [row for row in rows if row["path"] == "clause"]
        assert [tuple(row[field] for field in ROW_FIELDS) for row in got_rules] == [
            (
                row["item_id"],
                row["finding"],
                by_id[row["item_id"]]["impact"],
                by_id[row["item_id"]]["certainty"],
            )
            for row in rule_rows
        ], f"{spec.name}: 规则条目的内容或顺序漂移"
        assert [(row["clause_ref"], row["finding"]) for row in got_clauses] == [
            (str(row["paragraph"]), row["reason"]) for row in clause_rows
        ], f"{spec.name}: 条款结论的内容或顺序漂移"
        assert len(rows) == len(merged)

    # 过程开关关闭（默认）：第三层一个字段都不返回，第一层一条不少
    assert detail["process"] is None
    assert verdict["process_details"] == []
    assert all(row["process"] is None for row in rows)
    assert detail["disclaimer"] == presenters.RESULT_DISCLAIMER
    assert detail["workflow"]["state"] == "history"
    expected_first, _expected_process = presenters.split_verdict_details(expected["verdict"]["details"])
    assert verdict["conclusion_details"] == expected_first, f"{spec.name}: 第一层覆盖明细漂移"


@pytest.mark.parametrize("spec", FIXTURES, ids=[spec.name for spec in FIXTURES])
def test_verdict_caveat_comes_from_server(spec: FixtureSpec, replayed) -> None:
    """`VerdictView.caveat` 必须是服务端常量（006 结果 DTO 表），前端不得另写一份。

    四个夹具走两条不同渲染路径（模型主导 / 旧 `merge_findings`），这里逐个确认：
    结论提醒与路径无关、与过程开关无关，且不是空串——空串会让前端"看起来没有提醒"。
    """
    api, _state, build = replayed
    _task, stored = build(spec)
    detail = _get_detail(api.client, stored.task_id)

    caveat = detail["verdict"]["caveat"]
    assert caveat == presenters.VERDICT_CAVEAT, f"{spec.name}: verdict.caveat 不是服务端常量"
    assert caveat.strip(), f"{spec.name}: verdict.caveat 不能为空"


@pytest.mark.parametrize("spec", FIXTURES, ids=[spec.name for spec in FIXTURES])
def test_process_switch_only_adds_third_layer(spec: FixtureSpec, replayed) -> None:
    """打开 `ui.show_query_path` 只增第三层：第一二层逐条不变。"""
    api, state, build = replayed
    _task, stored = build(spec)
    expected = _expected(spec)
    off = _get_detail(api.client, stored.task_id)

    response = api.client.put(
        "/api/settings/ui",
        json={"show_query_path": True},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 200, response.text
    on = _get_detail(api.client, stored.task_id)

    # 第一层：结论句、计数、条目、限制、建议、必备条款、事实、类案逐项相同
    assert on["verdict"]["status"] == off["verdict"]["status"]
    assert on["verdict"]["headline"] == off["verdict"]["headline"]
    assert on["verdict"]["risk_count"] == off["verdict"]["risk_count"]
    assert on["verdict"]["review_count"] == off["verdict"]["review_count"]
    assert on["verdict"]["conclusion_details"] == off["verdict"]["conclusion_details"]
    # 结论提醒属第一层：开关只加第三层，`caveat` 前后都是同一个服务端常量
    assert on["verdict"]["caveat"] == off["verdict"]["caveat"] == presenters.VERDICT_CAVEAT
    assert on["limits"] == off["limits"]
    assert on["suggestions"] == off["suggestions"]
    assert on["facts"] == off["facts"]
    assert on["cases"] == off["cases"]
    assert on["disclaimer"] == off["disclaimer"]
    assert [(row["item_id"], row["finding"], row["impact"], row["certainty"]) for row in on["findings"]] == [
        (row["item_id"], row["finding"], row["impact"], row["certainty"]) for row in off["findings"]
    ]
    # 必备条款：只有"已写 x 类"是覆盖率口径，按开关出现，其余计数不变
    assert on["missing_terms"]["absent_count"] == off["missing_terms"]["absent_count"]
    assert on["missing_terms"]["unseen_count"] == off["missing_terms"]["unseen_count"]
    assert on["missing_terms"]["manual_count"] == off["missing_terms"]["manual_count"]
    assert off["missing_terms"]["written_count"] is None

    # 第三层：过程块、覆盖明细、每条来源齐全
    assert on["process"] is not None
    assert off["process"] is None
    assert (
        on["verdict"]["process_details"]
        == presenters.split_verdict_details(expected["verdict"]["details"])[1]
    )
    assert set(on["verdict"]["conclusion_details"]) | set(on["verdict"]["process_details"]) == set(
        expected["verdict"]["details"]
    )
    assert all(row["process"] is not None for row in on["findings"])
    process = on["process"]
    assert process["task_id"] == stored.task_id
    assert process["latency_s"] is not None or process["latency_text"] == "未记录"
    assert process["run_detail"].startswith("任务 ")
    assert process["traceability_text"] in ("引用可溯源", "引用不可溯源")


@pytest.mark.parametrize("spec", FIXTURES, ids=[spec.name for spec in FIXTURES])
def test_report_bytes_match_frozen_text(spec: FixtureSpec, replayed) -> None:
    """经 API 下载的 Word：正文（段落 + 表格）与 `*.report.txt` 归一化后完全一致。"""
    api, _state, build = replayed
    _task, stored = build(spec)
    expected = _expected(spec)

    response = api.client.get(f"/api/tasks/{stored.task_id}/report")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].split(";")[0] == DOCX_MIME
    disposition = response.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition
    assert response.content[:2] == b"PK"

    want = normalize_ws((FIXTURES_DIR / expected["report"]).read_text(encoding="utf-8"))
    got = normalize_ws(_report_text(response.content, stored.task_id))
    assert want, f"{spec.name}: 报告快照是空的"
    assert len(want) > 200, f"{spec.name}: 报告快照过短"
    assert got == want, f"{spec.name}: API 下载的报告正文与 v0.3.2 快照不一致"
