"""验收阈值与降级策略常量。评测脚本引用此处的数值。"""

from __future__ import annotations

ACCEPTANCE: dict[str, float] = {
    "accuracy": 0.75,  # 风险/缺项识别准确率
    "recall": 0.80,  # 风险/缺项召回率
    "traceable": 1.00,  # 依据可追溯率
    "ask_trigger": 0.90,  # 事实补问触发率
    "no_false_ask": 0.85,  # 不误问率
    "refuse": 0.90,  # 越界拒答率
    "false_refuse": 0.15,  # 误拒率（上限）
    "latency_s": 90.0,  # 首轮处理时间（上限）
    "unsourced_suggestion": 0.00,  # 建议无依据率（上限）
}

# 单轮追问与可补充事实的条数上限：阻断项最多三项，一次问全、不分多轮
MAX_ASK_ITEMS = 3
# 可补充事实的候选字段池上限（先按规则影响排序，再由模型在池内挑）
MAX_ASK_CANDIDATES = 8

DISCLAIMER_SUGGESTION = "非法律意见，仅供参考；重大争议请咨询专业人士。"
DISCLAIMER_CASE = "同类情形通常如何认定，不构成本案结论。"
DECLARED_REFUSAL = "该问题超出劳动用工范围，建议咨询专业人士或拨打 12348 公共法律服务热线。"
