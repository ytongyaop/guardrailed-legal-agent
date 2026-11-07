"""界面用的小数据加工（纯函数，可单测；不 import streamlit，避免副作用）。"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

import pandas as pd


def _stamp(value) -> str:
    """把任务时间转成图上可读的标签：有则到分钟，旧记录只有日期。"""
    if isinstance(value, datetime):
        if value.hour == 0 and value.minute == 0:
            return value.strftime("%m-%d")
        return value.strftime("%m-%d %H:%M")
    return str(value or "未知")


def trend_frame(tasks: list) -> pd.DataFrame:
    """把最近若干次核对整理成趋势表（时间正序，最多 12 条）。

    - `次序`：数值列，保证图里顺序稳定（不按字符串排序）；
    - `标签`：图上 x 轴显示的时间；同一时间的多条记录追加序号，避免被图表聚合；
    - `日期`：原始时间戳（tooltip 用），不编造时分。
    """
    rows = []
    for idx, task in enumerate(reversed(list(tasks)[:12]), start=1):
        stamp = _stamp(task.created_at)
        rows.append(
            {
                "次序": idx,
                "标签": stamp,
                "风险条目": len(task.items),
                "处理建议": len(task.suggestions),
                "日期": stamp,
            }
        )
    counts = Counter(r["标签"] for r in rows)
    seen: dict[str, int] = {}
    for row in rows:
        label = row["标签"]
        if counts[label] > 1:
            seen[label] = seen.get(label, 0) + 1
            row["标签"] = f"{label} · {seen[label]}"
    return pd.DataFrame(rows)
