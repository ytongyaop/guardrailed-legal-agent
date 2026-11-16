"""L2 辅助层（合同要素骨架）测试：数据形态、校验脚本的禁用项、payload 注入与段落归类。

全部离线：只读包内 YAML 与仓库自带的对抗合同，不联网、不花钱。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from mla.config import data_path
from mla.domain.clauses import ClauseCandidate
from mla.domain.elements import (
    ELEMENT_IDS,
    FALLBACK_ELEMENT_ID,
    IMPACTS,
    classify_paragraph,
    default_elements,
    load_elements,
    match_element_id,
)
from mla.domain.models import StatuteEntry
from mla.services.clause_review import REVIEW_SYSTEM, _payload

ELEMENTS_PATH = Path(data_path("clauses/elements.yaml"))
VALIDATOR_PATH = Path(__file__).resolve().parents[1] / "scripts" / "validate_elements.py"
TRAPS_01 = Path(data_path("evalsets/contract_traps_01.txt"))

ELEMENTS = default_elements()
# 本文件只测要素骨架的形状与归类，法条正文不参与断言：空库即为最省事的桩
EMPTY_LIBRARY: dict[str, StatuteEntry] = {}


def _load_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("validate_elements", VALIDATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = _load_validator()


def _paragraphs(text: str) -> list[str]:
    """按非空行分段（与 `domain.clauses` / `clause_review._non_empty_lines` 同口径）。"""
    return [line.strip() for line in text.splitlines() if line.strip()]


def _candidate(paragraph: int, span: str, trigger_id: str = "TR-TEST") -> ClauseCandidate:
    return ClauseCandidate(
        paragraph=paragraph,
        span=span,
        trigger_id=trigger_id,
        name="测试候选",
        candidate_statute_ids=("LCL-35",),
        severity_hint="A",
    )


# ---------- 数据形态 ----------


def test_elements_yaml_loads_thirteen_unique_ids() -> None:
    """骨架能加载、13 条、ID 唯一且恰好是 EL-1..EL-13（顺序即叙述顺序）。"""
    elements = load_elements(ELEMENTS_PATH)

    assert len(elements) == len(ELEMENT_IDS) == 13
    assert tuple(e.element_id for e in elements) == ELEMENT_IDS
    assert len({e.element_id for e in elements}) == 13
    for element in elements:
        assert element.name.strip()
        assert element.impact in IMPACTS
        assert len(element.section_hints) >= 2


def test_every_element_has_three_to_five_focus_items() -> None:
    """每个要素的 focus 3–5 条，每条 8–40 字（提示是给模型的判断方向，不是写几条都行）。"""
    for element in ELEMENTS:
        assert 3 <= len(element.focus) <= 5, element.element_id
        for item in element.focus:
            assert 8 <= len(item) <= 40, f"{element.element_id}: {item}"


def test_focus_items_carry_no_numbers_or_article_ids() -> None:
    """骨架里的名称与判断方向不含数值、条号、写法枚举（V19 的内容口径，脚本另有独立实现）。"""
    for element in ELEMENTS:
        texts = [element.name, *element.focus]
        for text in texts:
            for banned in ("第", "%", "倍", "元", "LCL-", "包括但不限于", "等等"):
                assert banned not in text, f"{element.element_id}: {text}"

    dumped = json.dumps([{"name": e.name, "focus": list(e.focus)} for e in ELEMENTS], ensure_ascii=False)
    assert not any(ch.isdigit() for ch in dumped), "名称与判断方向里不该出现阿拉伯数字"


# ---------- 校验脚本：四个禁止项各一个反例 ----------


def _write_with_bad_focus(tmp_path: Path, bad_focus: str, element_id: str = "EL-3") -> Path:
    """把真实骨架里某个要素的一条 focus 换成违规文本，落成临时 YAML。"""
    rows = yaml.safe_load(ELEMENTS_PATH.read_text(encoding="utf-8"))
    target = next(row for row in rows if row["element_id"] == element_id)
    target["focus"][0] = bad_focus
    path = tmp_path / "bad_elements.yaml"
    path.write_text(yaml.safe_dump(rows, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def test_validator_accepts_real_file() -> None:
    """正向基线：真实骨架必须通过校验器（否则下面的反例没有意义）。"""
    assert validator.main(["--elements", str(ELEMENTS_PATH)]) == 0


@pytest.mark.parametrize(
    ("bad_focus", "label"),
    [
        ("试用期不得超过 6 个月，否则视为超档", "禁数字"),
        ("依据第十九条判断试用期是否超出档位", "禁条号"),
        ("对照 LCL-19 判断试用期是否超出档位", "禁法条 ID"),
        ("包括但不限于试用期工资与社保的交代", "禁枚举引导"),
    ],
)
def test_validator_rejects_banned_forms(
    tmp_path: Path, bad_focus: str, label: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """四个禁止项各一个反例：校验脚本必须判失败（退出码 1）并指出是哪一类。"""
    path = _write_with_bad_focus(tmp_path, bad_focus)

    assert validator.main(["--elements", str(path)]) == 1

    captured = capsys.readouterr().out
    assert label in captured, captured
    assert "EL-3" in captured


def test_validator_rejects_missing_element(tmp_path: Path) -> None:
    """少一个要素也要判失败（13 条是硬要求）。"""
    rows = yaml.safe_load(ELEMENTS_PATH.read_text(encoding="utf-8"))
    path = tmp_path / "short.yaml"
    path.write_text(yaml.safe_dump(rows[:-1], allow_unicode=True, sort_keys=False), encoding="utf-8")

    assert validator.main(["--elements", str(path)]) == 1


def test_validator_rejects_bad_impact(tmp_path: Path) -> None:
    """影响度越域（高/中/低之外）判失败。"""
    rows = yaml.safe_load(ELEMENTS_PATH.read_text(encoding="utf-8"))
    rows[0]["impact"] = "极高"
    path = tmp_path / "bad_impact.yaml"
    path.write_text(yaml.safe_dump(rows, allow_unicode=True, sort_keys=False), encoding="utf-8")

    assert validator.main(["--elements", str(path)]) == 1


# ---------- payload 注入：只发本批出现的要素 ----------


def _payload_for(paragraphs: list[int], contract: str) -> dict[str, Any]:
    lines = _paragraphs(contract)
    batch = [_candidate(p, lines[p - 1], trigger_id=f"TR-P{p}") for p in paragraphs]
    return _payload(batch, contract, EMPTY_LIBRARY, 3)


def test_payload_elements_only_contains_batch_elements() -> None:
    """一批三条候选分属三个要素 → elements 只有这三个，不是 13 个全表。"""
    contract = TRAPS_01.read_text(encoding="utf-8")
    payload = _payload_for([20, 32, 46], contract)

    ids = [row["element_id"] for row in payload["elements"]]
    assert ids == ["EL-4", "EL-7", "EL-12"], "只发本批出现过的要素，且按骨架顺序"
    assert len(payload["elements"]) < len(ELEMENTS)
    assert all(row["focus"] for row in payload["elements"])

    # 每条候选都指向 elements 里的一项，且每个下发要素都被至少一条候选用到（不发无用项）
    referenced = {row["element"] for row in payload["candidates"]}
    assert referenced == set(ids)
    for row in payload["candidates"]:
        assert row["element"] in ids


def test_payload_elements_are_deduplicated_and_stable() -> None:
    """同一批里多条候选同属一个要素 → 只发一次；顺序按骨架文件顺序（payload 可复现）。"""
    contract = TRAPS_01.read_text(encoding="utf-8")
    payload = _payload_for([20, 21, 23, 24], contract)

    ids = [row["element_id"] for row in payload["elements"]]
    assert ids == ["EL-4", "EL-5"]
    assert [row["element"] for row in payload["candidates"]] == ["EL-4", "EL-5", "EL-5", "EL-5"]


def test_payload_elements_have_no_numbers_or_article_ids() -> None:
    """V16 的形状断言：每项含 element_id / name / focus / impact，且名称与方向不含数值与条号。"""
    contract = TRAPS_01.read_text(encoding="utf-8")
    payload = _payload_for([20, 32, 46], contract)

    for row in payload["elements"]:
        assert set(row) == {"element_id", "name", "impact", "focus"}
        assert row["element_id"].startswith("EL-")
        text = row["name"] + "".join(row["focus"])
        assert not any(ch.isdigit() for ch in text), text
        assert "第" not in text
        assert "%" not in text


def test_payload_without_skeleton_sends_nothing() -> None:
    """骨架缺失（空序列）→ elements 为空列表、候选不带 element 字段（回滚口径：删文件即不注入）。"""
    contract = TRAPS_01.read_text(encoding="utf-8")
    lines = _paragraphs(contract)
    payload = _payload([_candidate(20, lines[19])], contract, EMPTY_LIBRARY, 3, elements=())

    assert payload["elements"] == []
    assert "element" not in payload["candidates"][0]


# ---------- 段落归类：用真实合同段落 ----------


@pytest.mark.parametrize(
    ("paragraph", "expected"),
    [
        (20, "EL-4"),  # 单方调岗调地
        (32, "EL-7"),  # 放弃缴纳社会保险
        (46, "EL-12"),  # 排除仲裁、只能起诉
        (43, "EL-9"),  # 全员竞业限制
        (29, "EL-6"),  # 延迟发放工资
    ],
)
def test_real_contract_paragraphs_classified(paragraph: int, expected: str) -> None:
    """真实对抗合同（`contract_traps_01`）的段落归类：命中要素特征词的段落各归其位。"""
    line = _paragraphs(TRAPS_01.read_text(encoding="utf-8"))[paragraph - 1]
    assert match_element_id(line, ELEMENTS) == expected, line


def test_unmatched_paragraph_falls_back_to_other_terms() -> None:
    """一个特征词都没命中 → 兜底 EL-13，且 `matched=False` 供调用方记诊断。"""
    element, matched = classify_paragraph("本合同一式两份，甲乙双方各执一份。", ELEMENTS)

    assert element is not None
    assert element.element_id == FALLBACK_ELEMENT_ID
    assert matched is False


def test_fallback_paragraph_recorded_in_diagnostics() -> None:
    """兜底归类要把段落号写进诊断（影响度按兜底取"中"，事后要能看出是兜底）。"""
    contract = "本合同一式两份，甲乙双方各执一份。\n"
    errors: list[str] = []
    payload = _payload(
        [_candidate(1, "本合同一式两份，甲乙双方各执一份。")],
        contract,
        EMPTY_LIBRARY,
        3,
        diagnostics=errors,
    )

    assert payload["candidates"][0]["element"] == FALLBACK_ELEMENT_ID
    assert any("第 1 段未命中要素骨架" in e for e in errors), errors


# ---------- prompt 说明 ----------


def test_review_system_explains_elements_are_hints_not_conclusions() -> None:
    """system 里说明 elements 是"合同哪部分、该盯哪些方向"，并明确不是结论、不要复述。"""
    assert "elements" in REVIEW_SYSTEM
    assert "该盯哪些方向" in REVIEW_SYSTEM
    assert "不是结论" in REVIEW_SYSTEM
    assert "不要逐条复述" in REVIEW_SYSTEM
    # 与既有 rule_checks / fact_sheet 的口径不冲突：两者仍"只作判断参考"
    assert "只作判断参考" in REVIEW_SYSTEM
