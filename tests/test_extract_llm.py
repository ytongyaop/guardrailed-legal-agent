"""LLM 辅助抽取测试：全部用 stub，不联网（规格 2.8 第 1-7 条 + 成本口径 1 条）。"""

from __future__ import annotations

from typing import Any

import pytest

from mla.adapters.extract import extract_facts
from mla.adapters.extract_llm import LLMExtractor, _normalize_value
from mla.adapters.llm import LLMError

NO_KEY = "本次未使用模型抽取：未配置 Key"


class StubGenerator:
    """假的生成器：返回固定 payload 或抛指定异常，并按 DeepSeekLLM 的口径累加 token。"""

    available = True

    def __init__(self, payload: Any = None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls = 0
        self.last_system = ""
        self.last_user = ""
        self.usage_in = 0
        self.usage_out = 0

    def generate(self, system: str, user: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls += 1
        self.last_system = system
        self.last_user = user
        if self.error is not None:
            raise self.error
        self.usage_in += 10
        self.usage_out += 5
        return self.payload

    def classify(self, text: str, labels: list[str]) -> str:
        return "不确定"


def test_llm_only_fills_missing_fields() -> None:
    text = "乙方试用期为六个月。工资每季度发放一次。"
    stub = StubGenerator(
        {
            "facts": {
                "probation_months": {"value": "3", "evidence": "试用期为六个月"},
                "pay_times_per_month": {"value": "0.25", "evidence": "每季度发放"},
            }
        }
    )
    out = LLMExtractor(stub).extract(text)

    assert out.facts["probation_months"] == "6"  # 正则为王
    assert out.facts["pay_times_per_month"] == "0.25"  # 模型补齐
    assert any("抽取冲突" in e for e in out.errors)
    assert out.degraded is False
    # 只补缺失：正则已得的键要写进 user prompt
    assert "probation_months=6" in stub.last_user
    assert "不要重复输出" in stub.last_user


def test_out_of_range_field_dropped() -> None:
    text = "试用期为二〇个月，每年享受五天年休假。"
    stub = StubGenerator(
        {
            "facts": {
                "probation_months": {"value": "99", "evidence": "二〇个月"},
                "annual_leave_days": {"value": "5", "evidence": "五天年休假"},
            }
        }
    )
    out = LLMExtractor(stub).extract(text)

    assert "probation_months" not in out.facts
    assert out.facts["annual_leave_days"] == "5"  # 其余合法字段保留
    assert any("probation_months" in e and "区间" in e for e in out.errors)
    assert out.degraded is False


def test_evidence_not_in_text_dropped() -> None:
    text = "乙方试用期为陆个月。"
    stub = StubGenerator({"facts": {"probation_months": {"value": "7", "evidence": "试用期为七个月"}}})
    out = LLMExtractor(stub).extract(text)

    assert "probation_months" not in out.facts
    assert any("证据" in e for e in out.errors)
    assert out.degraded is False


def test_evidence_whitespace_normalized() -> None:
    text = "每年\u3000享有\n五天年休假。"
    stub = StubGenerator({"facts": {"annual_leave_days": {"value": "5", "evidence": "五天 年休假"}}})
    out = LLMExtractor(stub).extract(text)

    assert out.facts["annual_leave_days"] == "5"
    assert out.evidence["annual_leave_days"] == "五天 年休假"


def test_invalid_json_falls_back_to_regex() -> None:
    text = "乙方试用期为六个月。"
    failed = LLMExtractor(StubGenerator(error=LLMError("模型返回不可用：bad json"))).extract(text)
    assert failed.degraded is True
    assert failed.errors
    assert any("回退规则式" in e for e in failed.errors)
    assert failed.facts == extract_facts(text)

    # 返回不是对象 → 按无法解析回退
    not_object = LLMExtractor(StubGenerator(["not", "an", "object"])).extract(text)
    assert not_object.degraded is True
    assert any("无法解析" in e for e in not_object.errors)
    assert not_object.facts == extract_facts(text)


def test_no_generator_falls_back_without_crash() -> None:
    out = LLMExtractor(None).extract("试用期为六个月")

    assert out.facts == {"probation_months": "6"}
    assert out.degraded is True
    assert NO_KEY in out.errors


def test_long_contract_truncated_not_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    """超过 max_chars 只截断送给模型的正文，不降级（截断属于成本护栏，不是失败）。"""
    monkeypatch.setenv("MLA_EXTRACT_MAX_CHARS", "50")
    text = (
        "甲方（用人单位）与乙方（劳动者）经平等自愿、协商一致，订立本劳动合同。"
        "乙方试用期为六个月。"
        "本合同其他条款均不影响上述约定，双方确认已充分理解全部内容并按约履行。"
    )
    stub = StubGenerator({"facts": {}})
    out = LLMExtractor(stub).extract(text)

    assert out.degraded is False
    assert any("合同超过 50 字" in e for e in out.errors)
    # 实现细节：prompt 前缀是「已抽取字段…合同正文：」，只有正文被截到 50 字
    assert "合同正文：\n" in stub.last_user
    sent = stub.last_user.split("合同正文：\n", 1)[1]
    assert sent == text[:50]
    assert len(sent) == 50
    assert out.facts["probation_months"] == "6"  # 截断后仍命中的正则结果保留


def test_enum_value_must_match_exactly() -> None:
    text = "甲方聘用乙方，合同期限为三年。"
    loose = LLMExtractor(
        StubGenerator({"facts": {"employer_type": {"value": "有限公司", "evidence": "甲方聘用乙方"}}})
    ).extract(text)
    assert "employer_type" not in loose.facts
    assert any("枚举" in e for e in loose.errors)

    exact = LLMExtractor(
        StubGenerator({"facts": {"employer_type": {"value": "有限责任公司", "evidence": "甲方聘用乙方"}}})
    ).extract(text)
    assert exact.facts["employer_type"] == "有限责任公司"


def test_token_usage_accumulates_on_generator() -> None:
    stub = StubGenerator({"facts": {}})
    extractor = LLMExtractor(stub)
    out = extractor.extract("乙方试用期为六个月。")

    assert extractor.generator is stub  # 复用同一实例，token 才能合并计数
    assert stub.usage_in == 10
    assert stub.usage_out == 5
    assert out.degraded is False


def test_term_type_enum_whitelist() -> None:
    """合同期限类型是枚举字段：只认 固定期限 / 无固定期限，同义词丢弃。"""
    # 正文没有"固定期限 / 无固定期限"字面，正则是抽不到的，只能靠模型补
    text = "合同期限：自用工之日起至法定终止情形出现时止，试用期为三个月。"
    evidence = "自用工之日起至法定终止情形出现时止"
    kept = LLMExtractor(
        StubGenerator({"facts": {"term_type": {"value": "无固定期限", "evidence": evidence}}})
    ).extract(text)
    assert kept.facts["term_type"] == "无固定期限"
    assert kept.degraded is False

    dropped = LLMExtractor(
        StubGenerator({"facts": {"term_type": {"value": "永久", "evidence": evidence}}})
    ).extract(text)
    assert "term_type" not in dropped.facts
    assert any("枚举" in e and "term_type" in e for e in dropped.errors)


def test_pay_times_word_table_covers_quarter_half_and_biweekly() -> None:
    """发薪频次词表要能归一化季度 / 半年 / 两周（半年发一次等于每月 0.5 次）。"""
    assert _normalize_value("pay_times_per_month", "每季度") == "0.25"
    assert _normalize_value("pay_times_per_month", "每半年") == "0.5"
    assert _normalize_value("pay_times_per_month", "每两周") == "2"
    assert _normalize_value("pay_times_per_month", "每月") == "1"

    # 端到端：模型补的「每半年发一次」不再被五道校验丢成缺字段
    out = LLMExtractor(
        StubGenerator(
            {"facts": {"pay_times_per_month": {"value": "每半年", "evidence": "每半年发放一次工资"}}}
        )
    ).extract("甲方每半年发放一次工资。")
    assert out.facts["pay_times_per_month"] == "0.5"
