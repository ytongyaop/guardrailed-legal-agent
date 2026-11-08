"""抽取层测试：中文数字、关键项识别、抽不到不写。"""

from __future__ import annotations

from pathlib import Path

from mla.adapters.extract import cn_to_int, extract_facts
from mla.config import data_path

CONTRACT = Path(data_path("evalsets/sample_contract.txt"))


def test_cn_to_int() -> None:
    assert cn_to_int("六") == 6
    assert cn_to_int("十二") == 12
    assert cn_to_int("二十四") == 24
    assert cn_to_int("两") == 2
    assert cn_to_int("6") == 6
    assert cn_to_int("abc") is None


def test_cn_to_int_zero_matches_llm_layer() -> None:
    """正则档 `零` 必须解析为 0（与 `extract_llm._cn_int` 口径统一），空串仍 → None。"""
    assert cn_to_int("零") == 0
    assert cn_to_int("") is None


def test_extract_from_sample_contract() -> None:
    f = extract_facts(CONTRACT.read_text(encoding="utf-8"))
    assert f["probation_months"] == "12"
    assert f["term_years"] == "3"
    assert f["probation_wage_ratio"] == "70"
    assert f["written_contract"] == "否"
    assert f["months_without_contract"] == "2"
    assert f["noncompete_years"] == "4"
    assert f["ot_hours_month"] == "40"
    assert f["social_insurance"] == "否"
    assert f["employer_type"] == "有限责任公司"


def test_empty_text_yields_nothing() -> None:
    assert extract_facts("") == {}


def test_unknown_text_does_not_guess() -> None:
    f = extract_facts("双方本着平等自愿的原则签订本合同。")
    assert "probation_months" not in f
    assert "term_years" not in f


def test_term_type_from_contract() -> None:
    """合同期限类型：无固定期限合同没有 term_years，但要能判出期限类型。"""
    assert extract_facts("双方订立无固定期限劳动合同")["term_type"] == "无固定期限"
    assert extract_facts("固定期限三年")["term_type"] == "固定期限"
    # "无固定期限"字面里也含"固定期限"，不能被误判成固定期限
    f = extract_facts("双方订立无固定期限劳动合同，试用期七个月")
    assert f["term_type"] == "无固定期限"
    assert "term_years" not in f


def test_probation_with_chinese_colon() -> None:
    """冒号不能让试用期数字抽不到（对抗合同「试用期：6 个月」）。"""
    assert extract_facts("试用期：6 个月，自 2026 年 03 月 01 日起。")["probation_months"] == "6"
    assert extract_facts("试用期:6个月")["probation_months"] == "6"
    assert extract_facts("试用期，六个月")["probation_months"] == "6"


def test_probation_with_spaces() -> None:
    """数字前后带空格也要抽到（_norm 负责去空白）。"""
    assert extract_facts("试用期 6 个月")["probation_months"] == "6"
    assert extract_facts("本合同试用期为 六 个月。")["probation_months"] == "6"


def test_probation_with_yuedingwei() -> None:
    """「约定为」「约定」是常见连接词，不能被挡死。"""
    assert extract_facts("双方试用期约定为 6 个月")["probation_months"] == "6"
    assert extract_facts("试用期约定六个月")["probation_months"] == "6"


def test_social_insurance_negation_fangqi() -> None:
    """「放弃缴纳社会保险」是否定式，不能被肯定式吞成「是」。"""
    f = extract_facts("乙方自愿申请放弃缴纳社会保险，甲方将社保单位部分折算为现金补贴 1000 元。")
    assert f["social_insurance"] == "否"


def test_social_insurance_negation_bujiao() -> None:
    """「不缴社保」是否定式。"""
    assert extract_facts("公司不缴社保。")["social_insurance"] == "否"


def test_social_insurance_negation_weijiao() -> None:
    """「未缴纳社会保险」是否定式。"""
    assert extract_facts("公司未为员工缴纳社会保险。")["social_insurance"] == "否"


def test_social_insurance_positive_yijiao() -> None:
    """「已缴纳」不能被误判成否定式。"""
    assert extract_facts("公司已为员工缴纳社会保险。")["social_insurance"] == "是"


def test_social_insurance_positive_yifa() -> None:
    """「依法缴纳」不能被误判成否定式。"""
    assert extract_facts("甲方依法为乙方缴纳社会保险。")["social_insurance"] == "是"


# ---------- 社保否定式的转嫁与未参保写法 ----------


def test_cn_to_int_capital_numerals() -> None:
    """大写数字（柒/捌/玖）与小写同口径。"""
    assert cn_to_int("柒") == 7
    assert cn_to_int("捌") == 8
    assert cn_to_int("玖") == 9


def test_social_insurance_negation_self_pay() -> None:
    """由乙方自行缴纳 / 承担属于转嫁表述，必须判「否」，不能只看否定词。"""
    assert extract_facts("乙方应当自行缴纳社会保险，甲方发放补贴。")["social_insurance"] == "否"
    # 否定词落在另一小句（「甲方不再另行缴纳」后面没有社保），只能靠转嫁口径判
    assert extract_facts("社会保险费用由乙方自行承担，甲方不再另行缴纳。")["social_insurance"] == "否"
    # 「社保由个人缴纳」也是转嫁写法（合法的写法是「由用人单位代扣代缴」）
    assert extract_facts("社会保险由个人缴纳。")["social_insurance"] == "否"


def test_social_insurance_negation_not_participating() -> None:
    """未参加社会保险属于未参保，必须判「否」；未办理参保手续则不算。"""
    assert extract_facts("乙方未参加社会保险，由个人处理。")["social_insurance"] == "否"


def test_social_insurance_positive_when_employer_pays() -> None:
    """用人单位缴纳时必须是「是」，含「乙方应当缴纳的部分由甲方代扣代缴」这种合法写法。"""
    assert extract_facts("甲方依法为乙方缴纳社会保险。")["social_insurance"] == "是"
    text = "甲方为乙方办理有关社会保险手续，乙方应当缴纳的社会保险费由甲方从工资中代扣代缴。"
    assert extract_facts(text)["social_insurance"] == "是"


def test_ot_hours_bu_de_chaoguo() -> None:
    """「不得超过 / 最多 / 上限为」也要能抽到加班上限。"""
    assert extract_facts("每月加班不得超过三十六小时。")["ot_hours_month"] == "36"
    assert extract_facts("每月加班最多 36 小时")["ot_hours_month"] == "36"


def test_probation_with_capital_numerals() -> None:
    """「试用期为柒个月」要抽成 7。"""
    assert extract_facts("双方约定试用期为柒个月。")["probation_months"] == "7"


def test_probation_wage_fullwidth_percent() -> None:
    """全角百分号「％」也要能抽。"""
    assert extract_facts("试用期工资为 80％ 的转正工资。")["probation_wage_ratio"] == "80"
