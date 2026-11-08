"""越界判定测试（纯规则，不联网）。"""

from __future__ import annotations

from mla.domain.boundary import classify_by_terms


def test_in_scope_terms() -> None:
    d = classify_by_terms("劳动合同约定试用期十二个月合法吗")
    assert d.in_scope is True
    assert d.layer == "terms"


def test_out_of_scope_terms() -> None:
    d = classify_by_terms("离婚财产怎么分割")
    assert d.in_scope is False
    assert d.layer == "terms"


def test_unknown_defaults_to_refuse() -> None:
    d = classify_by_terms("今天天气怎么样")
    assert d.in_scope is False
    assert d.layer == "default"
