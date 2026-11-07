"""指标计算：纯函数，零依赖，可单测。"""

from __future__ import annotations

from collections.abc import Sequence

from ..domain.models import CheckItem, Suggestion
from ..domain.policy import ACCEPTANCE

# 越大越好的指标；其余（误拒率 / 时延 / 无依据率）是上限
LOWER_IS_BETTER = ("false_refuse", "latency_s", "unsourced_suggestion")


def precision_recall(predicted: set[str], gold: set[str]) -> tuple[float, float]:
    """返回 (准确率, 召回率)。空集按“完全正确”处理，避免除零。"""
    if not predicted and not gold:
        return 1.0, 1.0
    tp = len(predicted & gold)
    precision = tp / len(predicted) if predicted else 0.0
    recall = tp / len(gold) if gold else 1.0
    return precision, recall


def micro_accuracy(predicted: set[str], gold: set[str]) -> float:
    """单样本准确率＝Jaccard（命中 / 并集），用于逐条取平均。"""
    if not predicted and not gold:
        return 1.0
    union = predicted | gold
    return len(predicted & gold) / len(union) if union else 1.0


def traceable_rate(items: Sequence[CheckItem], allowed: set[str]) -> float:
    """依据可追溯率：每条结论都要有 source_id，且能在允许集合里找到。"""
    if not items:
        return 1.0
    ok = sum(1 for i in items if i.source_id and i.source_id in allowed)
    return ok / len(items)


def unsourced_suggestion_rate(suggestions: Sequence[Suggestion]) -> float:
    """建议类条目无依据率（上限 0）。"""
    if not suggestions:
        return 0.0
    bad = sum(1 for s in suggestions if not s.source_ids)
    return bad / len(suggestions)


def rate(flags: Sequence[bool]) -> float:
    """命中比例；空集合返回 0（没有样本不能算达标）。"""
    return sum(1 for f in flags if f) / len(flags) if flags else 0.0


def report(metrics: dict[str, float]) -> dict[str, bool]:
    """对照 ACCEPTANCE 给出达标判定；未测的指标不出现在结果里。"""
    out: dict[str, bool] = {}
    for key, threshold in ACCEPTANCE.items():
        value = metrics.get(key)
        if value is None:
            continue
        out[key] = value <= threshold if key in LOWER_IS_BETTER else value >= threshold
    return out
