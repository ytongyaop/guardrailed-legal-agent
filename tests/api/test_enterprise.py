"""企业档案三项必选与事实预填（迁移自已删除的 `tests/test_ui_enterprise.py`）。

旧用例测 Streamlit 页面的 `enterprise_profile_errors` / `_facts_from_enterprise` / `fact_display_name`。
0.4.0 删界面后这些口径留在 API：`routes/enterprise.py:51` 的三项校验（缺项 400 + 字段名 + 中文名，
不落库）与 `presenters.enterprise_facts`（`POST /checks` 用它把已录事实预填进核对）。
这里用同一组断言重测：字段顺序、报错文案与预填口径都不能随删界面一起丢。
"""

from __future__ import annotations

import pytest

from mla.adapters.persist import SqliteRepository
from mla.api import presenters
from mla.domain.models import Enterprise, Role, User

from .api_helpers import csrf_of, error_of, setup_admin

pytestmark = pytest.mark.no_llm

THREE = {"employer_type": "个体工商户", "working_hours": "标准工时", "social_insurance": "是"}
REQUIRED_LABELS = ["employer_type", "working_hours", "social_insurance"]


def test_new_enterprise_leaves_three_fields_blank(api) -> None:
    """三项没有默认值：没填就是空串，不当成已填（交给追问）。"""
    setup_admin(api.client)
    ent = api.client.get("/api/enterprise").json()
    assert ent is not None
    assert (ent["employer_type"], ent["working_hours"], ent["social_insurance"]) == ("", "", "")


def test_save_requires_three_choices(api) -> None:
    """缺项：400 + 字段名 + 中文名，且不落库。"""
    state = setup_admin(api.client)
    headers = {"X-CSRF-Token": csrf_of(state)}

    none = api.client.put("/api/enterprise", json={"name": "某某科技"}, headers=headers)
    assert none.status_code == 400, none.text
    error = error_of(none)
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["fields"] == REQUIRED_LABELS
    assert error["message"] == "保存前请先选择：用工主体类型、工时制度、是否缴纳社保"
    assert api.client.get("/api/enterprise").json()["name"] == "测试企业", "校验失败不得改写已存档案"

    blank = api.client.put(
        "/api/enterprise", json={**THREE, "name": "某某科技", "social_insurance": "   "}, headers=headers
    )
    assert error_of(blank)["details"]["fields"] == ["social_insurance"], "只有空白的项才算缺项"


def test_saved_choices_round_trip(api) -> None:
    state = setup_admin(api.client)
    headers = {"X-CSRF-Token": csrf_of(state)}
    saved = api.client.put(
        "/api/enterprise", json={**THREE, "name": "某某科技", "staff_size": 8}, headers=headers
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["working_hours"] == "标准工时"
    assert api.client.get("/api/enterprise").json()["social_insurance"] == "是"


def test_facts_prefill_follows_archive(api) -> None:
    """档案里已录的三项预填进核对事实；空档案一条都不预填。"""
    setup_admin(api.client)
    enterprise_id = api.client.get("/api/enterprise").json()["enterprise_id"]
    repo = SqliteRepository(db_path=api.db_path)
    user = User(
        user_id="U1",
        username="admin",
        password_hash="x",
        salt="y",
        role=Role.ADMIN,
        enterprise_id=enterprise_id,
    )
    try:
        assert presenters.enterprise_facts(repo, user) == {}, "新档案不该预填任何事实"
        repo.save_enterprise(Enterprise(enterprise_id=enterprise_id, name="某某科技", **THREE))
        assert presenters.enterprise_facts(repo, user) == THREE
    finally:
        repo.close()
