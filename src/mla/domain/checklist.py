"""清单汇总：把抽取结果、规则命中、检索证据组装成"风险/缺项/待确认"，并生成建议。

建议的红线：**无依据不生成**（每条建议必须带 source_ids，由校验器复查）。
模板化建议不调模型；换模型生成时仍走同一校验。
"""

from __future__ import annotations

from .models import CaseRef, CheckItem, ItemKind, Suggestion

# 规则前缀 -> 建议模板（{name} 会被规则名替换）
SUGGESTION_TEMPLATES: dict[str, str] = {
    "R-PROBATION": "将试用期调整至法定上限内，并确认试用期工资不低于法定下限。",
    "R-WRITTEN": "尽快补签书面劳动合同，避免逾期产生二倍工资责任。",
    "R-NONCOMPETE": "核对竞业限制期限与补偿约定，超期或未付补偿的条款应整改。",
    "R-OT": "按法定倍率补足加班费，并保留考勤与工资计算依据。",
    "R-SI": "补办社会保险登记或补缴，留存缴费凭证。",
    "R-AL": "安排补休或按规定支付未休年休假工资报酬。",
    "R-WPR": "按约定周期足额支付工资，并保留支付记录。",
}
DEFAULT_SUGGESTION = "按对应法条调整该条款，并保留书面依据。"

DISCLAIMER = "非法律意见，仅供参考；重大争议请咨询专业人士。"


def _template_for(item: CheckItem) -> str:
    for prefix, text in SUGGESTION_TEMPLATES.items():
        if item.rule_id.startswith(prefix) or item.item_id.startswith(prefix):
            return text
    return DEFAULT_SUGGESTION


def build_suggestions(items: list[CheckItem], evidence: list | None = None) -> list[Suggestion]:
    """为每条风险生成一条建议；来源绑定该条目的法条 ID（保证可回链）。

    同一句建议可能由多条规则命中（如试用期超限 + 试用期工资偏低），
    这里按文案合并，来源取并集，避免界面出现重复条目。
    """
    out: list[Suggestion] = []
    index: dict[str, Suggestion] = {}
    for item in items:
        if item.kind is not ItemKind.RISK or not item.source_id:
            continue
        text = _template_for(item)
        if text in index:
            merged = tuple(dict.fromkeys((*index[text].source_ids, item.source_id)))
            index[text].source_ids = merged
            continue
        suggestion = Suggestion(text=text, source_ids=(item.source_id,), disclaimer=DISCLAIMER)
        index[text] = suggestion
        out.append(suggestion)
    return out


def attach_cases(items: list[CheckItem], cases: list, limit: int = 5) -> list[CaseRef]:
    """类案参照：按“命中条目引用的法条”与案例的 statute_ids 重合度排序。

    红线：案例只作参照，不参与结论，也不替代法条；没有重合就不硬挂。
    """
    cited = {i.source_id for i in items if i.source_id}
    if not cited or not cases:
        return []
    scored = []
    for case in cases:
        overlap = len(set(getattr(case, "statute_ids", ()) or ()) & cited)
        if overlap:
            scored.append((overlap, case))
    scored.sort(key=lambda pair: (-pair[0], pair[1].case_id))
    return [CaseRef(case_id=case.case_id, holding=case.holding) for _, case in scored[:limit]]
