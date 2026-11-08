"""越界判定（拒答判定层）：纯规则，不调模型，可单测。

两层设计（都实现在 `classify_by_terms` 里，目标：该拒的拒、不该拒的别误拒）：
- 第一层：命中劳动用工术语白名单 → 在范围内；命中明确越界词 → 直接拒答（`layer="terms"`）；
- 第二层：未命中任何领域词 → **保守拒答**（`layer="default"`，宁可少答，也不乱答）。

`boundary` 节点跑在 `retrieve` 之前，拿不到检索结果，所以没有"检索零命中兜底"这一层；
`facts`（只有结构化事实、无自由文本）与 `llm`（问答时模型判为越界）两处归因由节点另行标注。
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
    layer: str  # terms / default（指标归因用）
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
