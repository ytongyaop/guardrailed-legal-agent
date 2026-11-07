"""企业档案三项必填与事实显示名：档案不再用默认值顶替用户填写，界面不露原始 id。"""

from __future__ import annotations

import pytest

pytest.importorskip("streamlit")

from mla.adapters.persist import SqliteRepository  # noqa: E402
from mla.config import Paths  # noqa: E402
from mla.domain.models import Enterprise, User  # noqa: E402
from mla.ui.app import (  # noqa: E402
    _facts_from_enterprise,
    enterprise_profile_errors,
    fact_display_name,
    slot_table_rows,
)

THREE = {"employer_type": "个体工商户", "working_hours": "标准工时", "social_insurance": "否"}


def _repo(tmp_path) -> SqliteRepository:
    paths = Paths(statutes="data/statutes/statutes.jsonl", rules="data/rules/rules.yaml")
    return SqliteRepository(paths=paths, db_path=str(tmp_path / "ui.sqlite3"))


def _user(enterprise_id: str = "E1") -> User:
    return User(user_id="U1", username="alice", password_hash="x", salt="y", enterprise_id=enterprise_id)


def test_enterprise_defaults_are_blank() -> None:
    """三项没有默认值：没填就是空串，交给追问，不当成已填。"""
    ent = Enterprise(enterprise_id="E1", name="某某科技")
    assert ent.employer_type == ""
    assert ent.working_hours == ""
    assert ent.social_insurance == ""


def test_profile_errors_require_three_choices() -> None:
    """三项没选全就报缺项，返回的是中文名，不会落库。"""
    assert enterprise_profile_errors({}) == ("用工主体类型", "工时制度", "是否缴纳社保")
    assert enterprise_profile_errors(
        {"employer_type": None, "working_hours": "", "social_insurance": "   "}
    ) == ("用工主体类型", "工时制度", "是否缴纳社保")
    assert enterprise_profile_errors({**THREE, "social_insurance": None}) == ("是否缴纳社保",)
    assert enterprise_profile_errors(THREE) == ()


def test_new_enterprise_prefills_nothing(tmp_path) -> None:
    """新账号档案三项为空时，不预填任何事实，核对时会被追问。"""
    repo = _repo(tmp_path)
    repo.save_enterprise(Enterprise(enterprise_id="E1", name="某某科技"))
    assert _facts_from_enterprise(repo, _user()) == {}


def test_filled_enterprise_prefills_three_facts(tmp_path) -> None:
    repo = _repo(tmp_path)
    repo.save_enterprise(Enterprise(enterprise_id="E1", name="某某科技", **THREE))
    assert _facts_from_enterprise(repo, _user()) == THREE


def test_archive_field_names_are_chinese() -> None:
    """档案专用事实与规则字段都要显示中文名，查不到才原样返回 id。"""
    assert fact_display_name("working_hours") == "工时制度"
    assert fact_display_name("social_insurance") == "社保缴纳"
    assert fact_display_name("term_years") == "合同期限"
    assert fact_display_name("no_such_field") == "no_such_field"


def test_slot_table_shows_rule_ids(tmp_path) -> None:
    """管理页必问事实清单要带上真实规则 id，阻断项仍是三项。"""
    rows = slot_table_rows(_repo(tmp_path).list_rules())
    by_id = {row["槽位"]: row for row in rows}
    assert len(rows) == 21
    assert sum(1 for row in rows if row["类型"] == "阻断项") == 3
    assert "R-PROBATION-6M" in by_id["term_years"]["被哪些规则使用"]
    assert by_id["written_contract"]["被哪些规则使用"] == ""
