"""趋势表测试：纯函数，不依赖 Streamlit 运行时。"""

from __future__ import annotations

from datetime import datetime

from mla.domain.models import CheckItem, CheckTask, ItemKind, Suggestion, TaskMode
from mla.ui.trend import trend_frame


def _task(idx: int, risks: int) -> CheckTask:
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


def test_trend_frame_is_chronological() -> None:
    tasks = [_task(3, 3), _task(2, 2), _task(1, 1)]  # 仓储返回：最新在前
    frame = trend_frame(tasks)
    assert list(frame.columns) == ["次序", "标签", "风险条目", "处理建议", "日期"]
    assert list(frame["风险条目"]) == [1, 2, 3]  # 反转为时间顺序
    assert frame["次序"].tolist() == [1, 2, 3]
    # 旧记录当天 00:00 → 只显示日期；有时间的显示到分钟
    assert frame["标签"].tolist() == ["11-01", "11-02", "11-03"]


def test_trend_frame_shows_time_when_available() -> None:
    task = _task(3, 1)
    task.created_at = datetime(2026, 11, 3, 14, 5)
    assert trend_frame([task])["标签"].tolist() == ["11-03 14:05"]


def test_duplicate_timestamps_get_suffix() -> None:
    """同一天的多条旧记录：标签加序号，否则图表会把它们聚成一根柱。"""
    labels = trend_frame([_task(1, 1), _task(1, 2)])["标签"].tolist()
    assert labels == ["11-01 · 1", "11-01 · 2"]


def test_trend_frame_limits_to_twelve() -> None:
    tasks = [_task(i % 27 + 1, i) for i in range(20, 0, -1)]
    assert len(trend_frame(tasks)) == 12
