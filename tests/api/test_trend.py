"""统计与趋势的口径（迁移自已删除的 `tests/test_trend.py`）。

旧用例测 `mla.ui.trend.trend_frame`（Streamlit 侧的小数据加工）；0.4.0 删除界面后同一口径由
`api.presenters.history_trend` 提供（`GET /history/trend`）。这里按同一组断言重测，防止
「删界面顺手把趋势口径丢了」：最多 12 点、按时间正序、标签到分钟、同一时间的标签加序号。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from mla.api.presenters import history_trend
from mla.domain.models import CheckItem, CheckTask, ItemKind, Suggestion, TaskMode

pytestmark = pytest.mark.no_llm


def _task(idx: int, risks: int) -> CheckTask:
    """`idx` 只用来排时间；仓储口径是「新→旧」，所以调用方按倒序传入。"""
    return CheckTask(
        task_id=f"T{idx}",
        mode=TaskMode.CHECK,
        items=[
            CheckItem(item_id=str(i), kind=ItemKind.RISK, clause_ref="", finding="x", source_id="LCL-19")
            for i in range(risks)
        ],
        suggestions=[Suggestion(text="s", source_ids=("LCL-19",))] * risks,
        created_at=datetime(2026, 11, idx, 0, 0),
    )


def test_points_are_chronological() -> None:
    trend = history_trend([_task(3, 3), _task(2, 2), _task(1, 1)])
    assert [point.order for point in trend.points] == [1, 2, 3]
    assert [point.risk_item_count for point in trend.points] == [1, 2, 3]  # 入参新→旧，输出反转为时间正序
    assert [point.suggestion_count for point in trend.points] == [1, 2, 3]
    # 旧记录是 00:00，只显示日期
    assert [point.label for point in trend.points] == ["11-01", "11-02", "11-03"]


def test_label_keeps_minutes_when_available() -> None:
    task = _task(3, 1)
    task.created_at = datetime(2026, 11, 3, 14, 5)
    assert [point.label for point in history_trend([task]).points] == ["11-03 14:05"]


def test_duplicate_timestamps_get_suffix() -> None:
    """同一天的多条旧记录：标签加序号，否则图表会把它们聚成一根柱。"""
    labels = [point.label for point in history_trend([_task(1, 1), _task(1, 2)]).points]
    assert labels == ["11-01 · 1", "11-01 · 2"]


def test_window_keeps_latest_twelve() -> None:
    trend = history_trend([_task(i % 27 + 1, i) for i in range(20, 0, -1)])
    assert len(trend.points) == 12
    assert trend.recent == 20
    assert trend.max == 20
    assert trend.average == pytest.approx(14.5)
