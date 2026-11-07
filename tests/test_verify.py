"""校验器测试：结论/建议必须有依据；案例不得作结论依据。"""

from __future__ import annotations

from mla.domain.models import CheckItem, ItemKind, SourceKind, Suggestion, source_kind
from mla.domain.verify import verify

ALLOWED = {"LCL-19", "R-PROBATION-6M", "C-2024-01"}


def _item(source_id: str, item_id: str = "I1") -> CheckItem:
    return CheckItem(
        item_id=item_id,
        kind=ItemKind.RISK,
        clause_ref="",
        finding="…",
        source_id=source_id,
    )


def test_source_kind_prefixes() -> None:
    assert source_kind("LCL-19") is SourceKind.STATUTE
    assert source_kind("R-PROBATION-6M") is SourceKind.RULE
    assert source_kind("C-2024-01") is SourceKind.CASE


def test_statute_backed_item_passes() -> None:
    report = verify([_item("LCL-19")], [], ALLOWED)
    assert report.ok is True
    assert report.errors == ()


def test_item_without_source_fails() -> None:
    report = verify([_item("")], [], ALLOWED)
    assert report.ok is False
    assert "缺少依据" in report.errors[0]


def test_item_source_outside_allowed_fails() -> None:
    report = verify([_item("LCL-999")], [], ALLOWED)
    assert report.ok is False
    assert "不在本轮可用集合" in report.errors[0]


def test_case_cannot_back_conclusion() -> None:
    report = verify([_item("C-2024-01")], [], ALLOWED)
    assert report.ok is False
    assert "案例不得作为结论依据" in report.errors[0]


def test_suggestion_must_have_evidence() -> None:
    bad = Suggestion(text="建议补签书面合同", source_ids=())
    assert verify([], [bad], ALLOWED).ok is False
    good = Suggestion(text="建议补签书面合同", source_ids=("LCL-82",))
    assert verify([], [good], {"LCL-82"}).ok is True
