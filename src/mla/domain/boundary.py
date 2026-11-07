"""越界判定（拒答第一、三层）：纯规则，不调模型，可单测。

三层设计（目标：该拒的拒、不该拒的别误拒）：
- 第一层 `classify_by_terms`：劳动用工术语白名单 / 明确越界词 → 直接定向；
- 第二层 `zero_hit_fallback`：检索零命中 → 视为越界（在节点里调用检索器后判断）；
- 第三层 `default_deny`：两层都判不了 → **保守拒答**（宁可少答，也不乱答）。

返回 `BoundaryDecision(in_scope, layer, reason)`，`layer` 用于归因判定发生在哪一层。
"""

from __future__ import annotations

from dataclasses import dataclass

# 劳动用工领域关键词（命中任一 → 在范围内）
IN_SCOPE_TERMS: tuple[str, ...] = (
    "劳动合同",
    "试用期",
    "工资",
    "加班",
    "工时",
    "社保",
    "社会保险",
    "公积金",
    "竞业",
    "保密",
    "解除",
    "辞退",
    "离职",
    "经济补偿",
    "赔偿金",
    "年休假",
    "年假",
    "工伤",
    "女职工",
    "未成年工",
    "劳务派遣",
    "用工",
    "雇佣",
    "招聘",
    "考勤",
    "劳动仲裁",
    "劳动合同法",
    "劳动法",
)

# 明确越界词（命中且不含任何在范围内关键词 → 直接拒答）
OUT_OF_SCOPE_TERMS: tuple[str, ...] = (
    "离婚",
    "抚养",
    "继承",
    "房屋买卖",
    "租赁合同",
    "股权",
    "股东",
    "商标",
    "专利",
    "著作权",
    "刑事",
    "取保候审",
    "交通肇事",
    "行政处罚",
    "税务",
    "发票",
    "偷税",
    "贷款",
    "借款",
    "担保",
    "保险理赔",
    "医疗事故",
    "移民",
    "签证",
)


@dataclass(frozen=True)
class BoundaryDecision:
    """判定结果：in_scope=False 时应走拒答分支。"""

    in_scope: bool
    layer: str  # terms / zero_hit / default（指标归因用）
    reason: str


def classify_by_terms(text: str) -> BoundaryDecision:
    """第一层：术语判定。"""
    t = (text or "").strip()
    in_hits = [k for k in IN_SCOPE_TERMS if k in t]
    out_hits = [k for k in OUT_OF_SCOPE_TERMS if k in t]

    if in_hits:
        return BoundaryDecision(True, "terms", f"命中领域词：{in_hits[:3]}")
    if out_hits:
        return BoundaryDecision(False, "terms", f"命中越界词：{out_hits[:3]}")
    return BoundaryDecision(False, "default", "未命中任何领域词，保守拒答")


def zero_hit_fallback(hit_count: int, text: str) -> BoundaryDecision:
    """第二层：检索兜底。检索零命中且无领域词 → 越界。"""
    if hit_count > 0:
        return BoundaryDecision(True, "zero_hit", f"检索命中 {hit_count} 条")
    return BoundaryDecision(False, "zero_hit", "检索零命中")
