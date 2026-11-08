"""字段目录：中文名、追问句、填写提示，供界面与报告展示用。

三张字典同键：`FIELD_NAMES` / `FIELD_QUESTIONS` / `FIELD_HINTS`，键集合必须一致
（`tests/test_slots_derived.py` 守着这一点）。纯数据、纯函数，不读写文件、不联网。
查不到时原样返回 id，保证展示层不会因为目录缺项而中断。
"""

from __future__ import annotations

FIELD_NAMES: dict[str, str] = {
    "term_years": "合同期限",
    "work_years": "累计工作年限",
    "probation_months": "试用期月数",
    "annual_leave_days": "年休假天数",
    "term_type": "合同期限类型",
    "probation_times": "约定试用期次数",
    "probation_wage_ratio": "试用期工资比例",
    "months_without_contract": "未签书面合同月数",
    "noncompete_years": "竞业限制年限",
    "noncompete_unpaid_months": "未付竞业补偿月数",
    "ot_hours_day": "单日加班小时数",
    "ot_hours_month": "单月加班小时数",
    "ot_weekday_ratio": "工作日加班费倍率",
    "ot_restday_ratio": "休息日加班费倍率",
    "ot_holiday_ratio": "法定节假日加班费倍率",
    "days_to_si_register": "社保登记超期天数",
    "si_fine_multiple": "社保罚款倍率",
    "unused_leave_pay_ratio": "未休年假工资倍率",
    "pay_times_per_month": "每月发薪次数",
    "employer_type": "用工主体类型",
    "written_contract": "是否签订书面合同",
}

# 面向用户的追问句：12 至 40 字、以问号结尾、不引条号、不下结论。
FIELD_QUESTIONS: dict[str, str] = {
    "term_years": "合同约定的期限是几年（无固定期限填 0）？",
    "work_years": "员工累计工作年限是几年（含在其他单位的工作经历）？",
    "probation_months": "合同约定的试用期是几个月？",
    "annual_leave_days": "员工每年实际享受多少天年休假？",
    "term_type": "合同属于固定期限、无固定期限还是以完成一定工作为期限？",
    "probation_times": "同一员工在本单位约定过几次试用期？",
    "probation_wage_ratio": "试用期月工资约为转正后月工资的百分之多少？",
    "months_without_contract": "员工入职后多少个月仍未签订书面劳动合同？",
    "noncompete_years": "竞业限制约定的期限是几年？",
    "noncompete_unpaid_months": "单位已连续多少个月未支付竞业限制补偿？",
    "ot_hours_day": "员工单日最长加班多少小时？",
    "ot_hours_month": "员工单月累计加班多少小时？",
    "ot_weekday_ratio": "工作日加班按几倍工资计算？",
    "ot_restday_ratio": "休息日加班按几倍工资计算？",
    "ot_holiday_ratio": "法定节假日加班按几倍工资计算？",
    "days_to_si_register": "员工入职后多少天才办理社保登记？",
    "si_fine_multiple": "欠缴社保的罚款按欠缴数额的几倍计算？",
    "unused_leave_pay_ratio": "未休年假按几倍工资折算补偿？",
    "pay_times_per_month": "单位每月实际发放几次工资？",
    "employer_type": "您的经营主体是公司、个体工商户还是个人独资企业？",
    "written_contract": "是否已与员工签订书面劳动合同？",
}

# 字段缺失时的处置策略（未登记的字段一律按 external，保守处理）：
# - statutory：合同不必写，未写就按法定标准适用，规则不适用；
# - required：法律要求写进合同，没写就是缺项；
# - optional：约定才有，不约定即视为无此项；
# - external：合同外事实，要么问人，要么另行判断。
FIELD_ABSENCE: dict[str, str] = {
    # statutory：未约定按法定标准，规则不适用
    "probation_wage_ratio": "statutory",
    "ot_hours_day": "statutory",
    "ot_hours_month": "statutory",
    "ot_weekday_ratio": "statutory",
    "ot_restday_ratio": "statutory",
    "ot_holiday_ratio": "statutory",
    "si_fine_multiple": "statutory",
    "annual_leave_days": "statutory",
    "unused_leave_pay_ratio": "statutory",
    # required：法律要求约定，缺了算缺项
    "pay_times_per_month": "required",
    "term_years": "required",
    # optional：约定才有，没有即无此项
    "probation_months": "optional",
    "noncompete_years": "optional",
    # external：合同外事实，需要问人或另行判断
    "work_years": "external",
    "probation_times": "external",
    "months_without_contract": "external",
    "noncompete_unpaid_months": "external",
    "days_to_si_register": "external",
    "employer_type": "external",
    "written_contract": "external",
    "term_type": "external",
}
FIELD_ABSENCE_DEFAULT = "external"

# 填写提示：界面输入框占位用，写一条能照着填的示例或取值集合。
FIELD_HINTS: dict[str, str] = {
    "term_years": "例如：3（无固定期限填 0）",
    "work_years": "例如：8",
    "probation_months": "例如：3",
    "annual_leave_days": "例如：5",
    "term_type": "例如：固定期限 / 无固定期限 / 以完成一定工作任务为期限",
    "probation_times": "例如：1",
    "probation_wage_ratio": "例如：80（表示百分之八十）",
    "months_without_contract": "例如：2（入职当月已签填 0）",
    "noncompete_years": "例如：2",
    "noncompete_unpaid_months": "例如：3（按月支付填 0）",
    "ot_hours_day": "例如：3",
    "ot_hours_month": "例如：36",
    "ot_weekday_ratio": "例如：1.5",
    "ot_restday_ratio": "例如：2",
    "ot_holiday_ratio": "例如：3",
    "days_to_si_register": "例如：45",
    "si_fine_multiple": "例如：3",
    "unused_leave_pay_ratio": "例如：3",
    "pay_times_per_month": "例如：1",
    "employer_type": "例如：有限责任公司 / 个体工商户 / 个人独资企业",
    "written_contract": "填 是 或 否",
}


def field_name(field_id: str) -> str:
    """返回字段中文名；目录里没有该 id 时原样返回，不抛异常。"""
    return FIELD_NAMES.get(field_id, field_id)


def field_question(field_id: str) -> str:
    """返回字段的追问句；目录里没有该 id 时给一条通用问法，不留空。"""
    if field_id in FIELD_QUESTIONS:
        return FIELD_QUESTIONS[field_id]
    return f"{field_name(field_id)}的实际情况是什么？"


def field_hint(field_id: str) -> str:
    """返回字段填写提示；目录里没有该 id 时给空串，由界面自行兜底。"""
    return FIELD_HINTS.get(field_id, "")


def field_absence(field_id: str) -> str:
    """返回字段缺失时的处置策略；目录里没有该 id 时按 external 处理（保守）。"""
    return FIELD_ABSENCE.get(field_id, FIELD_ABSENCE_DEFAULT)
