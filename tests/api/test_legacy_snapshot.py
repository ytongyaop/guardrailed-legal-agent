"""批次 1「冻结基线」：把 v0.3.2 的领域输出钉死成 JSON / TXT 字面量（规格 006 批次 1）。

冻的是**四类不可回归的输出**，不冻界面：

1. `summarize_verdict` 的三态结论（状态、条数、headline、覆盖明细）；
2. `sort_items` 的顺序（影响度 → 确定性 → 来源 → 段落号，四键全序）；
3. `merge_findings` 的合并顺序（规则条目 → A → B → C，旧路径的呈现顺序）；
4. `build_report_docx` 的正文（段落 + 表格文字，去掉随机任务号与当天日期）。

刻意**不 import 界面子包（`mla` 的 `ui`）**：0.4.0 批次 6 会删除 `src/mla/ui/`，依赖 Streamlit
渲染结果的快照会随界面一起失效——那时"基线"就自动消失了，等于什么都没冻。AppTest 的元素树、
HTML 一律不进快照。

四个夹具各自守什么：

| 夹具 | 守的东西 |
| --- | --- |
| `model_first_rich` | 模型主导路径：多条结论跨 高/中/低 与 确定冲突/待确认，含程序追加的数值复核条目 |
| `model_first_empty` | 零条目但**不是** clean：「没查出来」与「没问题」必须说成两句不同的话 |
| `legacy_path` | 旧路径：items 只有规则条目，条款结论另行叠加，`merge_findings` 合并排序 |
| `mixed_grades` | 同一影响度内混合确定性，锁四键排序的「确定性 → 来源 → 段落号」 |

构造与录制代码在 `tests/api/fixtures/build_tasks.py`（`python tests/api/fixtures/build_tasks.py`
可重放录制）。本文件只负责"再造一次 → 和字面量比"。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
FIXTURES_DIR = HERE / "fixtures"
TESTS_DIR = HERE.parent
# 本目录不是包（与 tests/ 其它用例一致，不引入 __init__.py）：显式把 fixtures 与 tests 放进
# sys.path，`build_tasks` 与 `helpers` 才都能按顶层模块导入。
for _path in (str(FIXTURES_DIR), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from build_tasks import (  # noqa: E402  # pyright: ignore[reportMissingImports]
    FIXTURES,
    FixtureSpec,
    build_task,
    make_repo,
    normalize_ws,
    report_text,
    snapshot,
)

pytestmark = pytest.mark.mock_llm

# 每个夹具的"非空壳"下限：防的是快照被录成空数组后一路绿灯（那时断言其实什么都没测）。
# 数量只给下限（/ 上限只对"必须为空"的夹具），真实条数由 JSON 字面量逐字段锁死。
SHAPE_GUARDS: dict[str, dict[str, Any]] = {
    "model_first_rich": {
        "min_items": 6,
        "min_impacts": 3,
        "certainties": {"确定冲突", "待确认"},
        "min_clause_merge_rows": 1,
        "status": "risk",
    },
    "model_first_empty": {
        "max_items": 0,
        "exact_clause_merge_rows": 0,
        # 零条目还不够：必须是"覆盖不完整"的 partial，不能是 clean
        "status": "partial",
        "headline_contains": "覆盖不完整",
    },
    "legacy_path": {
        "min_items": 2,
        "model_first": False,
        "min_item_merge_rows": 1,
        "min_clause_merge_rows": 1,
        "status": "risk",
    },
    "mixed_grades": {
        "min_items": 2,
        "max_impacts": 1,  # 全部落在同一个影响度内，才叫"同一影响度内混合确定性"
        "certainties": {"确定冲突", "待确认"},
        "status": "risk",
    },
}

ITEMS = ("item_id", "finding", "impact", "certainty", "sort_key", "grade", "source", "element_id")
IMPACTS = {"高", "中", "低"}
CERTAINTIES = {"确定冲突", "待确认"}


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / f"{name}.snapshot.json").read_text(encoding="utf-8"))


def _guard(spec: FixtureSpec, payload: dict[str, Any]) -> None:
    """非空壳守卫：快照本身必须是有内容的，否则"逐字段相等"只是空对空。"""
    guard = SHAPE_GUARDS[spec.name]
    items = payload["sorted_items"]
    verdict = payload["verdict"]
    merged = payload["merge_findings"]
    item_rows = [row for row in merged if row["kind"] == "item"]
    clause_rows = [row for row in merged if row["kind"] == "clause_finding"]

    if "min_items" in guard:
        assert len(items) >= guard["min_items"], f"{spec.name}: 条目太少（{len(items)}），快照疑似空壳"
    if "max_items" in guard:
        assert len(items) <= guard["max_items"], f"{spec.name}: 条目应为空，实际 {len(items)}"
    if "min_impacts" in guard:
        assert len({row["impact"] for row in items}) >= guard["min_impacts"], f"{spec.name}: 影响度覆盖不足"
    if "max_impacts" in guard:
        assert len({row["impact"] for row in items}) <= guard["max_impacts"], f"{spec.name}: 影响度应只有一个"
    if "certainties" in guard:
        assert {row["certainty"] for row in items} == guard["certainties"], f"{spec.name}: 确定性覆盖不全"
    if "min_item_merge_rows" in guard:
        assert len(item_rows) >= guard["min_item_merge_rows"], f"{spec.name}: 合并结果里没有规则条目"
    if "min_clause_merge_rows" in guard:
        assert len(clause_rows) >= guard["min_clause_merge_rows"], f"{spec.name}: 合并结果里没有条款结论"
    if "exact_clause_merge_rows" in guard:
        assert len(clause_rows) == guard["exact_clause_merge_rows"], f"{spec.name}: 条款结论条数不符"
    if "status" in guard:
        assert verdict["status"] == guard["status"], f"{spec.name}: 状态应为 {guard['status']}"
    if "headline_contains" in guard:
        assert guard["headline_contains"] in verdict["headline"], f"{spec.name}: headline 不对"
    if "model_first" in guard:
        assert payload["task"]["model_first"] is guard["model_first"], f"{spec.name}: 路径标记不对"

    # 每条条目都得有四个展示字段：缺了说明快照根本没锁到展示层
    for row in items:
        for key in ITEMS:
            assert key in row, f"{spec.name}: 条目缺字段 {key}"
        assert row["item_id"] and row["finding"], f"{spec.name}: 条目主键或正文为空"
        assert row["impact"] in IMPACTS, f"{spec.name}: 影响度越界 {row['impact']!r}"
        assert row["certainty"] in CERTAINTIES, f"{spec.name}: 确定性越界 {row['certainty']!r}"
        assert len(row["sort_key"]) == 4, f"{spec.name}: 排序键不是四键"
        assert all(isinstance(part, int) for part in row["sort_key"]), f"{spec.name}: 排序键含非整数"
    assert verdict["headline"], f"{spec.name}: verdict 标题为空"


@pytest.mark.parametrize("spec", FIXTURES, ids=[spec.name for spec in FIXTURES])
def test_legacy_snapshot_matches(spec: FixtureSpec, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """重放夹具 → 三个领域输出的每个字段都必须与 JSON 字面量相同，Word 正文与 TXT 相同。"""
    expected = _load(spec.name)
    repo = make_repo(str(tmp_path / f"{spec.name}.sqlite3"))
    task = build_task(spec, patch=monkeypatch, repo=repo)
    actual = snapshot(task, repo)

    _guard(spec, actual)

    assert actual["verdict"] == expected["verdict"], f"{spec.name}: verdict 漂移"
    assert actual["sorted_items"] == expected["sorted_items"], f"{spec.name}: 排序结果漂移"
    assert actual["merge_findings"] == expected["merge_findings"], f"{spec.name}: 合并顺序漂移"
    # 路径标记与规模也锁住：只比 verdict / items 时，"路径被悄悄换掉"可能看不出来
    for key in ("model_first", "clause_review", "item_count", "clause_finding_count"):
        assert actual["task"][key] == expected["task"][key], f"{spec.name}: task.{key} 漂移"

    want_text = normalize_ws((FIXTURES_DIR / expected["report"]).read_text(encoding="utf-8"))
    got_text = normalize_ws(report_text(task, repo))
    assert want_text, f"{spec.name}: 报告快照是空的"
    assert len(want_text) > 200, f"{spec.name}: 报告快照过短，疑似只存了标题"
    assert got_text == want_text, f"{spec.name}: Word 报告正文漂移"


def test_replay_is_deterministic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """同一夹具连跑两次必须完全一致：随机任务号 / 时间 / 耗时都不许漏进快照。"""
    spec = next(item for item in FIXTURES if item.name == "model_first_rich")
    repo = make_repo(str(tmp_path / "deterministic.sqlite3"))
    first = snapshot(build_task(spec, patch=monkeypatch, repo=repo), repo)
    second = snapshot(build_task(spec, patch=monkeypatch, repo=repo), repo)
    assert first == second


def test_permissions_matrix_is_consistent() -> None:
    """权限矩阵（批次 2/3 的 API 测试要引用）自身必须自洽，否则引用方会照错表断言。

    只查**结构**（不查设计文档）：能力名必须有端点定义、管理员集合必须含普通用户集合、
    `forbidden_for_user` 必须正好是"管理员能做而普通用户不能做"的那部分、匿名能力不得混进用户集合。
    """
    matrix = json.loads((FIXTURES_DIR / "permissions.json").read_text(encoding="utf-8"))
    endpoints = matrix["endpoints"]
    anonymous = set(matrix["anonymous"])
    user = set(matrix["user"])
    admin = set(matrix["admin"])
    forbidden = set(matrix["forbidden_for_user"])

    all_caps = anonymous | user | admin | forbidden
    assert all_caps == set(endpoints), f"能力名与端点表不一致：{sorted(all_caps ^ set(endpoints))}"
    assert user <= admin, f"普通用户能力必须全部包含在管理员集合里：{sorted(user - admin)}"
    assert forbidden == admin - user, f"forbidden_for_user 应等于 admin - user：{sorted(admin - user)}"
    assert not (anonymous & user), f"匿名能力不得出现在用户集合：{sorted(anonymous & user)}"
    assert not (anonymous & admin), f"匿名能力不得出现在管理员集合：{sorted(anonymous & admin)}"
    for capability, rows in endpoints.items():
        assert rows, f"{capability}: 没有对应端点"
        for row in rows:
            method, _, path = row.partition(" ")
            assert method and path.startswith("/"), f"{capability}: 端点写法不对 {row!r}"


def test_snapshot_module_has_no_streamlit_dependency() -> None:
    """基线的"不依赖 Streamlit"必须是被检查的，而不是被注释声明的。

    只查 import 语句（不查注释/文档里提到的名字）：这里要防的是**代码依赖**，
    而解释"为什么不依赖"的文字反而必须允许存在。
    """
    forbidden = (
        re.compile(r"^\s*(?:from|import)\s+mla\.ui\b", re.MULTILINE),
        re.compile(r"^\s*(?:from|import)\s+streamlit\b", re.MULTILINE),
    )
    for path in (Path(__file__), FIXTURES_DIR / "build_tasks.py"):
        source = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            assert not pattern.search(source), f"{path.name} 导入了界面依赖：{pattern.pattern}"
