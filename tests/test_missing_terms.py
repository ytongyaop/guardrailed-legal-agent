"""必备条款缺项检测：规格解析、五份基线判定、阈值边界、逐行判定、缺项不进 items。"""

from __future__ import annotations

from pathlib import Path

import pytest

from mla.config import LLMConfig
from mla.domain.missing import detect_missing_terms, load_missing_terms
from mla.domain.models import CheckTask, MissingStatus, Role, TaskMode, User
from mla.services.tasks import run_check

SPECS = load_missing_terms(Path("data/clauses/missing_terms.yaml"))
EVAL_DIR = Path("data/evalsets")
TERM_IDS = [f"MT-{i}" for i in range(1, 10)]
NO_LLM = LLMConfig(api_key="")  # 测试不读 secrets、不联网
BASE_FACTS = {"employer_type": "有限责任公司", "written_contract": "是", "term_years": "3"}


def _read(name: str) -> str:
    return (EVAL_DIR / name).read_text(encoding="utf-8")


def _statuses(text: str, *, min_chars: int = 400) -> dict[str, str]:
    terms = detect_missing_terms(text, SPECS, min_chars=min_chars)
    return {t.term_id: str(t.status) for t in terms}


def _absent(text: str) -> list[str]:
    return [tid for tid, status in _statuses(text).items() if status == MissingStatus.ABSENT]


def _coverage(text: str) -> float | None:
    terms = detect_missing_terms(text, SPECS)
    return CheckTask(task_id="T", mode=TaskMode.CHECK, missing_terms=list(terms)).missing_coverage


def _pad_to(text: str, total: int) -> str:
    """补「。」把正文凑到指定字数（句号不会命中任何关键词，也不构成新增条款）。"""
    assert len(text) <= total
    return text + "。" * (total - len(text))


# ---------- 规格 ----------
def test_nine_specs_parse_in_order() -> None:
    assert [s.term_id for s in SPECS] == TERM_IDS
    assert len({s.term_id for s in SPECS}) == 9
    assert all(s.statute_id == "LCL-17" for s in SPECS)


def test_spec_judges_and_modes() -> None:
    by_id = {s.term_id: s for s in SPECS}
    assert by_id["MT-9"].judge == "not_judged"
    assert by_id["MT-9"].patterns == () and by_id["MT-9"].groups == ()
    for term_id in ("MT-3", "MT-6", "MT-7", "MT-8"):
        assert by_id[term_id].mode == "any" and by_id[term_id].patterns
    for term_id in ("MT-1", "MT-2", "MT-4", "MT-5"):
        assert by_id[term_id].mode == "all" and by_id[term_id].groups


def test_mt8_patterns_exclude_gongshang() -> None:
    """「工伤」是风险条款用语，不能算作劳动保护已写（否则 traps_03 的缺项会被掩盖）。"""
    mt8 = next(s for s in SPECS if s.term_id == "MT-8")
    assert "工伤" not in mt8.patterns


# ---------- 基线 ----------
@pytest.mark.parametrize(
    ("name", "absent", "coverage"),
    [
        ("contract_compliant_01.txt", [], 1.0),
        ("contract_traps_01.txt", [], 1.0),
        ("contract_traps_02.txt", ["MT-8"], 0.875),
        ("contract_traps_03.txt", ["MT-3", "MT-8"], 0.75),
    ],
)
def test_baseline_contracts(name: str, absent: list[str], coverage: float) -> None:
    text = _read(name)
    assert _absent(text) == absent, name
    assert _coverage(text) == coverage, name


def test_short_contract_is_all_manual() -> None:
    text = _read("sample_contract.txt")
    assert len(text) == 262
    terms = detect_missing_terms(text, SPECS)
    assert [str(t.status) for t in terms] == [MissingStatus.MANUAL] * 9
    assert {t.note for t in terms} == {"正文不足，未判定"}
    assert _coverage(text) is None


# ---------- 阈值边界 ----------
def test_threshold_261_chars_is_manual() -> None:
    text = _pad_to("甲方：某某公司\n", 261)
    assert len(text) == 261
    assert set(_statuses(text).values()) == {MissingStatus.MANUAL}
    assert _coverage(text) is None


def test_threshold_400_chars_is_judged() -> None:
    text = _pad_to(_read("sample_contract.txt"), 400)
    assert len(text) == 400
    statuses = _statuses(text)
    assert all(statuses[tid] != MissingStatus.MANUAL for tid in TERM_IDS[:-1])
    assert statuses["MT-9"] == MissingStatus.MANUAL  # 兜底条款不判，与字数无关


# ---------- 判据细节 ----------
def test_all_mode_needs_every_group() -> None:
    """只有岗位、没有工作地点 → MT-4 未命中（组是全文范围，缺一组就整类未命中）。"""
    text = _pad_to("甲方：某某公司\n乙方的岗位为数控机床操作工。\n", 400)
    statuses = _statuses(text)
    assert statuses["MT-4"] == MissingStatus.UNSEEN  # certainty=低 → 只请人工确认
    assert "工作地点" not in text


def test_all_mode_groups_may_hit_different_lines() -> None:
    text = _pad_to("甲方：某某公司\n岗位：门店服务员。\n工作地点：杭州区域各门店。\n", 400)
    assert _statuses(text)["MT-4"] == MissingStatus.WRITTEN


def test_line_by_line_keeps_title_from_counting() -> None:
    """标题行「合同期限」+ 正文行「干满一年」不能拼成已写（traps_03 的真实结构）。"""
    text = _pad_to("二、合同期限\n4. 先干着，干满一年再签书面劳动合同。\n", 400)
    assert _statuses(text)["MT-3"] == MissingStatus.ABSENT


def test_high_certainty_absent_vs_low_certainty_unseen() -> None:
    text = _pad_to("甲方：某某公司\n", 400)
    statuses = _statuses(text)
    assert statuses["MT-6"] == MissingStatus.ABSENT  # 劳动报酬 certainty=高
    assert statuses["MT-1"] == MissingStatus.UNSEEN  # certainty=低


# ---------- 接入 ----------
def test_missing_terms_never_enter_items() -> None:
    task = run_check(
        user=User(
            user_id="U1",
            username="alice",
            password_hash="x",
            salt="y",
            role=Role.USER,
            enterprise_id="E1",
        ),
        contract_text=_read("contract_traps_03.txt"),
        facts=dict(BASE_FACTS),
        save=False,
        llm=NO_LLM,
    )
    assert task.items, "traps_03 本应有规则风险条目"
    assert [t.term_id for t in task.missing_terms] == TERM_IDS
    assert {t.term_id for t in task.missing_terms if t.status == MissingStatus.ABSENT} == {"MT-3", "MT-8"}
    assert not [item for item in task.items if item.item_id.startswith("MT-")]
    assert task.missing_coverage == 0.75
