"""界面冒烟测试：用 Streamlit 官方 AppTest 真跑一遍脚本，断言没有异常。

覆盖：未登录时显示登录页；注册后进入主界面（st.navigation 多页导航）。
"""

from __future__ import annotations

import ast
import re
import uuid

import pytest

pytest.importorskip("streamlit")

from pathlib import Path

from streamlit.testing.v1 import AppTest  # noqa: E402

from mla.domain.models import Role  # noqa: E402

APP = str(Path(__file__).resolve().parents[1] / "src" / "mla" / "ui" / "app.py")


def _fresh_app(tmp_path, monkeypatch) -> AppTest:
    """把数据库指到临时目录，避免污染真实库。"""
    monkeypatch.setenv("MLA_DB", str(tmp_path / "ui.sqlite3"))
    return AppTest.from_file(APP, default_timeout=30)


def _input(at: AppTest, label: str):
    return next(x for x in at.text_input if x.label == label)


def test_login_page_renders(tmp_path, monkeypatch) -> None:
    at = _fresh_app(tmp_path, monkeypatch).run()
    assert not at.exception
    assert any("小微企业用工风险辅助系统" in t.value for t in at.title)
    assert any(i.label == "用户名" for i in at.text_input)


def test_required_copy_is_present() -> None:
    """必留文案守卫：免责、转介、边界声明、评测口径不能被删。"""
    text = Path(APP).read_text(encoding="utf-8")
    for required in ("非法律意见", "12348", "不代表合规", "标注说明", "不构成本案结论"):
        assert required in text, f"界面缺少必留文案：{required}"


def _string_constants() -> list[str]:
    """界面里的中文字面量（不含注释），用于断言对外文案本身。"""
    tree = ast.parse(Path(APP).read_text(encoding="utf-8"))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.search(r"[\u4e00-\u9fff]", node.value)
    ]


def test_clause_grade_copy_has_no_verdict_wording() -> None:
    """等级文案守卫：只说“与法条明文冲突”，不写“违法/无效”。

    与 CLAUSE_DISCLAIMER（效力认定以裁判为准）和 prompt 里“不要写该条款违法/无效”
    的硬性要求保持一致；统计行同样由 CLAUSE_GRADES 生成，不会再出现自造措辞。
    """
    strings = _string_constants()
    assert any("A 与法条明文冲突" in s for s in strings)
    for banned in ("明确违法", "违法/无效", "违法行为", "条款无效"):
        for s in strings:
            assert banned not in s, f"界面文案仍出现终局结论用语：{banned}（{s[:40]}）"


def test_admin_delete_copy_is_present() -> None:
    """管理端删除区文案守卫：不可恢复提示与二次确认标签不能被删。"""
    text = Path(APP).read_text(encoding="utf-8")
    for required in (
        "删除后该账号及其企业、合同、任务、日志一并移除，不可恢复；操作会写一条管理员日志。",
        "输入目标用户名以确认",
        "确认删除账号",
    ):
        assert required in text, f"管理端缺少删除区文案：{required}"


def test_register_then_landing(tmp_path, monkeypatch) -> None:
    at = _fresh_app(tmp_path, monkeypatch).run()
    name = "ui" + uuid.uuid4().hex[:6]
    _input(at, "用户名（≥3 字符）").set_value(name)
    _input(at, "密码（≥6 位）").set_value("secret123")
    next(b for b in at.button if b.label == "注册").click().run()

    assert not at.exception
    assert at.session_state["user"].username == name
    assert any("合同核对" in h.value for h in at.header)


NAV_SCRIPT = """
from mla.domain.models import Role, User
import streamlit as st
import mla.ui.app as ui_app

captured = {}


def fake_navigation(sections):
    captured.update(sections)
    return None


ui_app.st.navigation = fake_navigation
user = User(
    user_id="U1",
    username="nav",
    password_hash="x",
    salt="y",
    role=Role(ROLE_NAME),
    enterprise_id="E1",
)
ui_app._build_nav(None, user)
for group, pages in captured.items():
    st.markdown("GROUP:" + group + "=" + ",".join(page.title for page in pages))
default = [page.title for page in captured.get("工作台", []) if vars(page).get("_default")]
st.markdown("DEFAULT=" + ",".join(default))
"""


def _nav_lines(role: Role) -> list[str]:
    """在真实脚本上下文里跑 _build_nav，把分组与页面名打印成可断言的文本行。

    st.Page 需要脚本运行上下文才会补全标题等字段，所以不能直接裸调。
    """
    from mla.ui import app as ui_app

    original = ui_app.st.navigation
    script = NAV_SCRIPT.replace("ROLE_NAME", repr(str(role)))
    try:
        at = AppTest.from_string(script, default_timeout=60).run()
    finally:
        ui_app.st.navigation = original
    assert not at.exception
    return [m.value for m in at.markdown]


def _groups(lines: list[str]) -> list[str]:
    return [line[len("GROUP:") :].split("=", 1)[0] for line in lines if line.startswith("GROUP:")]


def test_navigation_groups_for_normal_user() -> None:
    """普通用户两组：工作台只放合同核对（默认页），管理放三页，问答不再独立成页。"""
    lines = _nav_lines(Role.USER)
    assert _groups(lines) == ["工作台", "管理"]
    assert "GROUP:工作台=合同核对" in lines
    assert "GROUP:管理=企业档案,合同管理,历史记录" in lines
    assert "DEFAULT=合同核对" in lines
    assert not any("受限问答" in line for line in lines)


def test_navigation_has_admin_group_only_for_admin() -> None:
    """管理员多出独立的第三组，且只有管理端一页。"""
    lines = _nav_lines(Role.ADMIN)
    assert _groups(lines) == ["工作台", "管理", "管理端"]
    assert "GROUP:管理端=管理端" in lines
    assert "DEFAULT=合同核对" in lines
    assert not any("受限问答" in line for line in lines)


RESULT_SCRIPT = """
from pathlib import Path

from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.domain.models import Role, User
from mla.services.tasks import run_check
from mla.ui.app import _render_task

repo = SqliteRepository(
    paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
    db_path=DB_PATH,
)
user = User(
    user_id="U1", username="ui", password_hash="x", salt="y", role=Role.USER, enterprise_id="E1"
)
text = Path(data_path("evalsets/contract_traps_03.txt")).read_text(encoding="utf-8")
task = run_check(user=user, contract_text=text, repo=repo, enterprise_id="E1", save=False)
_render_task(task, repo)
"""


MERGED_SCRIPT = """
from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.domain.models import CheckItem, CheckTask, ClauseFinding, ItemKind, TaskMode
from mla.ui.app import _render_task

repo = SqliteRepository(
    paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
    db_path=DB_PATH,
)
task = CheckTask(
    task_id="T-MERGE",
    mode=TaskMode.CHECK,
    enterprise_id="E1",
    items=[
        CheckItem(
            item_id="I1",
            kind=ItemKind.RISK,
            clause_ref="第十九条",
            finding="试用期约定超过法定上限",
            source_id="LCL-19",
            rule_id="R-PROBATION-6M",
        )
    ],
    clause_findings=[
        ClauseFinding(
            paragraph=3,
            span="试用期工资为转正工资的 60%。",
            grade="A",
            reason="该约定与第二十条关于试用期工资不得低于约定工资 80% 的规定不符",
            statute_ids=("LCL-20",),
            trigger_id="TR-PROBATION-WAGE-BASE",
        )
    ] if WITH_FINDING else [],
)
_render_task(task, repo, clause_review=CLAUSE_ON)
"""


def _merged_app(tmp_path, *, clause_on: bool, with_finding: bool) -> AppTest:
    script = (
        MERGED_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "ui.sqlite3")))
        .replace("CLAUSE_ON", "True" if clause_on else "False")
        .replace("WITH_FINDING", "True" if with_finding else "False")
    )
    return AppTest.from_string(script, default_timeout=60).run()


def _all_text(at: AppTest) -> str:
    return "\n".join(
        e.value
        for e in list(at.markdown) + list(at.caption) + list(at.error) + list(at.warning) + list(at.info)
    )


def test_check_result_merges_clause_findings(tmp_path) -> None:
    """核对结论内混排：规则条目带来源徽标排在最前，条款结论紧随其后，且不再单列区块。"""
    at = _merged_app(tmp_path, clause_on=True, with_finding=True)
    assert not at.exception

    markdowns = [m.value for m in at.markdown]
    rule_line = next(i for i, m in enumerate(markdowns) if "规则判定·确定性" in m)
    clause_line = next(i for i, m in enumerate(markdowns) if "模型审查·A" in m)
    assert rule_line < clause_line, "规则条目必须排在条款结论之前"
    assert not any("条款审查（模型辅助）" in m for m in markdowns)
    assert "试用期约定超过法定上限" in markdowns[rule_line]
    assert "A 与法条明文冲突" in markdowns[clause_line]

    texts = _all_text(at)
    assert "依据 LCL-19" not in texts  # 依据回链法条名，不直接露 ID
    assert "《劳动合同法》第十九条" in texts
    assert "该约定与第二十条关于试用期工资不得低于约定工资 80% 的规定不符" in texts


def test_clause_review_note_when_not_reviewed(tmp_path) -> None:
    """条款审查未启用时给一行覆盖说明，不得写成未发现问题。"""
    at = _merged_app(tmp_path, clause_on=False, with_finding=False)
    assert not at.exception

    texts = _all_text(at)
    assert "本次未做条款级审查，条款级风险未覆盖。" in texts
    assert "未发现问题" not in texts


def test_clause_review_note_when_nothing_found(tmp_path) -> None:
    """启用且模型未命中：说未发现可疑条款，而不是未发现问题。"""
    at = _merged_app(tmp_path, clause_on=True, with_finding=False)
    assert not at.exception

    texts = _all_text(at)
    assert "本次条款级审查未发现可疑条款。" in texts
    assert "未发现问题" not in texts


HISTORY_SCRIPT = """
from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.domain.models import CheckTask, TaskMode
from mla.ui.app import _render_task

repo = SqliteRepository(
    paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
    db_path=DB_PATH,
)
task = CheckTask(task_id="T-HIST", mode=TaskMode.CHECK, enterprise_id="E1", clause_review=FLAG)
_render_task(task, repo)
"""


def _history_app(tmp_path, *, clause_review: bool) -> AppTest:
    script = HISTORY_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "ui.sqlite3"))).replace(
        "FLAG", "True" if clause_review else "False"
    )
    return AppTest.from_string(script, default_timeout=60).run()


def test_clause_review_note_uses_persisted_flag(tmp_path) -> None:
    """历史详情不传开关，按落库的 clause_review 说：做过就“未发现”，没做才“未覆盖”。"""
    reviewed = _history_app(tmp_path, clause_review=True)
    assert not reviewed.exception
    assert "本次条款级审查未发现可疑条款。" in _all_text(reviewed)

    skipped = _history_app(tmp_path, clause_review=False)
    assert not skipped.exception
    assert "本次未做条款级审查，条款级风险未覆盖。" in _all_text(skipped)


def test_check_result_shows_missing_terms(tmp_path, monkeypatch) -> None:
    """结果页必备条款区块：中文条款名 + 依据 ID，且不出现内部编号 MT-*。"""
    script = RESULT_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "ui.sqlite3")))
    at = AppTest.from_string(script, default_timeout=60).run()
    assert not at.exception

    texts = "\n".join(
        e.value
        for e in list(at.markdown) + list(at.caption) + list(at.error) + list(at.warning) + list(at.info)
    )
    assert "必备条款完整性" in texts
    assert "劳动合同期限" in texts
    assert "劳动保护与劳动条件" in texts
    assert "LCL-17" in texts
    assert "MT-" not in texts  # 内部编号不露给用户
    assert "合同无效" not in texts

    metrics = {m.label: m.value for m in at.metric}
    assert metrics["必备条款"] == "6/8"


CHECK_PAGE_SCRIPT = """
from pathlib import Path

import streamlit as st
from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.domain.models import Role, User
from mla.services.tasks import run_check
import mla.ui.app as ui_app

repo = SqliteRepository(
    paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
    db_path=DB_PATH,
)
user = User(
    user_id="U1", username="ui", password_hash="x", salt="y", role=Role.USER, enterprise_id="E1"
)
st.session_state["user"] = user
if WITH_TASK:
    text = Path(data_path("evalsets/contract_traps_03.txt")).read_text(encoding="utf-8")
    st.session_state["last_task"] = run_check(
        user=user, contract_text=text, repo=repo, enterprise_id="E1", save=False
    )
    st.session_state["ask_clause_review"] = False
ui_app._page_check(repo, user)
"""


def _check_page(tmp_path, *, with_task: bool) -> AppTest:
    script = CHECK_PAGE_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "ui.sqlite3"))).replace(
        "WITH_TASK", "True" if with_task else "False"
    )
    return AppTest.from_string(script, default_timeout=60).run()


def test_check_page_empty_state_shows_guide(tmp_path) -> None:
    """无结果时左栏出输入表单，右栏给引导文案，不把整页撑长。"""
    at = _check_page(tmp_path, with_task=False)
    assert not at.exception

    markdowns = "\n".join(m.value for m in at.markdown)
    assert "合同输入" in markdowns
    assert any(b.label == "开始核对" for b in at.button)
    texts = _all_text(at)
    assert "左侧粘贴或选择一份合同，点击开始核对，结果显示在这里。" in texts
    assert "可补充事实" not in texts


def test_check_page_right_column_shows_latest_result(tmp_path) -> None:
    """有结果时右栏渲染核对结论，可补充事实只在左栏出现一次。"""
    at = _check_page(tmp_path, with_task=True)
    assert not at.exception

    markdowns = [m.value for m in at.markdown]
    assert any("核对结论" in m for m in markdowns)
    assert sum(1 for m in markdowns if "可补充事实" in m) == 1
    assert "左侧粘贴或选择一份合同，点击开始核对，结果显示在这里。" not in _all_text(at)
    assert not any(h.value == "核对结果" for h in at.subheader)


def test_result_render_alone_has_no_optional_facts(tmp_path) -> None:
    """结果区单独渲染（历史回看）不再带可补充事实，避免重复出现。"""
    script = RESULT_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "ui.sqlite3")))
    at = AppTest.from_string(script, default_timeout=60).run()
    assert not at.exception
    assert "可补充事实" not in _all_text(at)


ENTERPRISE_SCRIPT = """
import streamlit as st
from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.domain.models import Role, User
import mla.ui.app as ui_app

repo = SqliteRepository(
    paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
    db_path=DB_PATH,
)
user = User(
    user_id="U-ENT", username="entuser", password_hash="x", salt="y", role=Role.USER, enterprise_id=""
)
st.session_state["user"] = user
ui_app._page_enterprise(repo, user)
"""


def test_enterprise_save_requires_three_choices(tmp_path) -> None:
    """三项不预选：没选就点保存只报错，不写库。"""
    script = ENTERPRISE_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "ui.sqlite3")))
    at = AppTest.from_string(script, default_timeout=60).run()
    assert not at.exception
    assert [s.value for s in at.selectbox] == [None, None, None]

    next(b for b in at.button if b.label == "保存档案").click().run()
    assert not at.exception
    assert any("保存前请先选择" in e.value for e in at.error)
    # 保存成功后会把企业绑定到账号；这里没绑，说明确实没落库
    assert at.session_state["user"].enterprise_id == ""


QA_PANEL_SCRIPT = """
from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.domain.models import CheckTask, Role, Suggestion, TaskMode, User
import mla.ui.app as ui_app

repo = SqliteRepository(
    paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
    db_path=DB_PATH,
)
user = User(
    user_id="U1", username="ui", password_hash="x", salt="y", role=Role.USER, enterprise_id="E1"
)
task = CheckTask(
    task_id="T-QA",
    mode=TaskMode.CHECK,
    enterprise_id="E1",
    facts={"term_years": "3"},
    suggestions=[Suggestion(text="把试用期调整到法定上限内。", source_ids=("LCL-19",))],
)


def fake_run_qa(**kwargs):
    import streamlit as st

    st.session_state["captured_context"] = kwargs.get("context")
    return CheckTask(
        task_id="T-QA2",
        mode=TaskMode.QA,
        enterprise_id="E1",
        suggestions=[Suggestion(text="试用期最长不得超过六个月。", source_ids=("LCL-19",))],
    )


ui_app.run_qa = fake_run_qa
ui_app._render_task(task, repo, user)
"""


def test_check_page_offers_contract_qa(tmp_path) -> None:
    """核对结果底部有可折叠的提问区；提问后渲染结论与依据，上下文不带合同正文。"""
    script = QA_PANEL_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "ui.sqlite3")))
    at = AppTest.from_string(script, default_timeout=60).run()
    assert not at.exception

    labels = [e.label for e in at.get("expander")]
    assert "就本合同继续提问" in labels
    assert "回答基于本次核对的结构化事实与命中法条，不发送合同正文。" in _all_text(at)

    next(x for x in at.text_area if x.label == "问题").set_value("试用期约定有什么风险？")
    next(b for b in at.button if b.label == "提问").click().run()
    assert not at.exception

    context = at.session_state["captured_context"]
    assert set(context) == {"facts", "items", "clauses"}
    assert "contract_text" not in context
    assert "结论与依据（法条原文）" in _all_text(at)
    assert any("试用期最长不得超过六个月。" in m.value for m in at.markdown)
