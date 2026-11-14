"""界面冒烟测试：用 Streamlit 官方 AppTest 真跑一遍脚本，断言没有异常。

覆盖：空库显示首次初始化页；有管理员时登录页只留标题；注册后进入主界面（st.navigation 多页导航）。
"""

from __future__ import annotations

import ast
import re
import uuid

import pytest

pytest.importorskip("streamlit")

from pathlib import Path

import streamlit as st
from streamlit.testing.v1 import AppTest  # noqa: E402

from mla.adapters.persist import SqliteRepository  # noqa: E402
from mla.domain.models import Role, User  # noqa: E402
from mla.services.auth import hash_password  # noqa: E402

APP = str(Path(__file__).resolve().parents[1] / "src" / "mla" / "ui" / "app.py")


def _fresh_app(tmp_path, monkeypatch) -> AppTest:
    """把数据库指到临时目录，避免污染真实库。

    `get_config` / `get_repo` 是 st.cache_resource：不清缓存的话，同一进程里后一个测试
    会继续用第一个测试库的路径（实测过），断言就会串味。
    """
    monkeypatch.setenv("MLA_DB", str(tmp_path / "ui.sqlite3"))
    st.cache_resource.clear()
    return AppTest.from_file(APP, default_timeout=30)


def _seed_admin(tmp_path, monkeypatch, *, onboarded: bool = True) -> User:
    """直接在 AppTest 会读的那个库里建管理员，用来测“已经初始化过”的分支。"""
    monkeypatch.setenv("MLA_DB", str(tmp_path / "ui.sqlite3"))
    repo = SqliteRepository(db_path=str(tmp_path / "ui.sqlite3"))
    digest, salt = hash_password("secret123")
    user = User(
        user_id="U-ADMIN",
        username="admin",
        password_hash=digest,
        salt=salt,
        role=Role.ADMIN,
        onboarded=onboarded,
    )
    repo.create_user(user)
    return user


def _input(at: AppTest, label: str):
    return next(x for x in at.text_input if x.label == label)


def test_init_page_renders_without_admin(tmp_path, monkeypatch) -> None:
    """空库先初始化：显示管理员创建表单，不显示登录表单。"""
    at = _fresh_app(tmp_path, monkeypatch).run()
    assert not at.exception
    assert any("首次使用" in t.value for t in at.title)
    labels = [i.label for i in at.text_input]
    for label in ("管理员账号", "登录密码", "确认密码", "企业名称"):
        assert label in labels
    assert not any(b.label == "登录" for b in at.button)
    assert not any(b.label == "注册" for b in at.button)


def test_login_page_renders(tmp_path, monkeypatch) -> None:
    _seed_admin(tmp_path, monkeypatch)
    at = _fresh_app(tmp_path, monkeypatch).run()
    assert not at.exception
    assert any("小微企业用工风险辅助系统" in t.value for t in at.title)
    assert any(i.label == "用户名" for i in at.text_input)


def test_login_page_is_minimal(tmp_path, monkeypatch) -> None:
    """登录页只留图标 + 标题 + 登录/注册：副标题、功能项、边界句都挪到首次引导弹窗。"""
    _seed_admin(tmp_path, monkeypatch)
    at = _fresh_app(tmp_path, monkeypatch).run()
    assert not at.exception
    texts = _all_text(at)
    for dropped in (
        "劳动合同逐条核对，结论回链法条原文。",
        "逐条核对",
        "依据可核验",
        "缺事实先追问",
        "仅处理劳动用工与劳动合同问题，越界直接拒答。",
    ):
        assert dropped not in texts, f"登录页仍保留已删除的说明：{dropped}"


def test_admin_eval_design_notes_removed() -> None:
    """评测页只留数据（样本数、生成时间），两条设计说明不再出现。"""
    text = Path(APP).read_text(encoding="utf-8")
    for dropped in ("本页只读展示最近一次结果", "阈值快照一并写入历史"):
        assert dropped not in text, f"评测页仍保留设计说明：{dropped}"


def test_required_copy_is_present() -> None:
    """必留文案守卫：免责、转介、边界声明、评测口径不能被删。"""
    text = Path(APP).read_text(encoding="utf-8")
    for required in (
        "非法律意见",
        "12348",
        "不代表合规",
        "标注说明",
        "不构成本案结论",
        "条款效力以仲裁机构或法院裁判为准",
    ):
        assert required in text, f"界面缺少必留文案：{required}"


def test_verify_badge_copy_is_traceability_not_pass() -> None:
    """绿标只说“引用可溯源”：它校验的是引用能不能回链，不是“合同没问题”。"""
    text = Path(APP).read_text(encoding="utf-8")
    assert "引用可溯源" in text
    assert "引用不可溯源" in text
    assert "依据校验通过" not in text


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
    _seed_admin(tmp_path, monkeypatch)
    at = _fresh_app(tmp_path, monkeypatch).run()
    name = "ui" + uuid.uuid4().hex[:6]
    _input(at, "用户名（≥3 字符）").set_value(name)
    _input(at, "密码（≥8 位）").set_value("secret123")
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
    """普通用户两组：工作台只放合同核对（默认页），管理放四页（含设置），问答不再独立成页。"""
    lines = _nav_lines(Role.USER)
    assert _groups(lines) == ["工作台", "管理"]
    assert "GROUP:工作台=合同核对" in lines
    assert "GROUP:管理=企业档案,合同管理,历史记录,设置" in lines
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


def test_result_top_shows_verdict_not_bare_count(tmp_path) -> None:
    """顶部结论：规则条目与 A 级条款结论相加（1+1=2），不再把条目数当裸指标。"""
    at = _merged_app(tmp_path, clause_on=True, with_finding=True)
    assert not at.exception

    markdowns = [m.value for m in at.markdown]
    assert any("发现 2 条与法条存在冲突的事项" in m for m in markdowns)
    assert not any(m.label == "风险条目" for m in at.metric), "条目数不再单独显示为裸数字"


def test_clause_review_note_when_nothing_found(tmp_path) -> None:
    """启用且模型未命中：说未发现可疑条款，而不是未发现问题。"""
    at = _merged_app(tmp_path, clause_on=True, with_finding=False)
    assert not at.exception

    texts = _all_text(at)
    assert "本次条款级审查未发现可疑条款。" in texts
    assert "未发现问题" not in texts


RESULT_DISCLAIMER_TEXT = "以上为风险提示与依据定位，不构成法律意见；条款效力以仲裁机构或法院裁判为准。"


def _disclaimer_count(at: AppTest) -> int:
    return sum(1 for c in at.caption if c.value == RESULT_DISCLAIMER_TEXT)


def test_result_disclaimer_always_once(tmp_path) -> None:
    """免责常驻结果页底部：开/不开条款审查、有没有条款结论，都只有一行。"""
    for clause_on, with_finding in ((True, True), (True, False), (False, False)):
        at = _merged_app(tmp_path, clause_on=clause_on, with_finding=with_finding)
        assert not at.exception
        assert _disclaimer_count(at) == 1, f"条款审查={clause_on} 条款结论={with_finding} 时免责不是一行"

    plain = _merged_app(tmp_path, clause_on=False, with_finding=False)
    assert [c.value for c in plain.caption][-1] == RESULT_DISCLAIMER_TEXT, "免责必须在所有区块之后"
    assert "违法" not in RESULT_DISCLAIMER_TEXT and "无效" not in RESULT_DISCLAIMER_TEXT


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
    assert "第十七条" in texts
    assert "LCL-" not in texts  # 内部编号与前缀不露给用户
    assert "MT-" not in texts  # 内部编号不露给用户
    assert "合同无效" not in texts

    metrics = {m.label: m.value for m in at.metric}
    assert "必备条款" not in metrics, "必备条款不再是裸指标，改由顶部覆盖明细说"
    assert "必备条款 6/8 已写" in texts


CHECK_PAGE_SCRIPT = """
from pathlib import Path
from unittest.mock import patch

import streamlit as st
from mla.adapters.persist import SqliteRepository
from mla.config import LLMConfig, Paths, data_path
from mla.domain.models import Role, User
from mla.services.tasks import run_check
import mla.ui.app as ui_app

repo = SqliteRepository(
    paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
    db_path=DB_PATH,
)
user = User(
    user_id="U1", username="ui", password_hash="x", salt="y", role=Role(ROLE_NAME), enterprise_id="E1"
)
st.session_state["user"] = user
# 直接调用页面函数时 _build_nav 没跑，NAV 为空；门禁里的设置页链接需要它存在。
ui_app.NAV["设置"] = st.Page(lambda: None, title="设置", url_path="settings")
if WITH_TASK:
    text = Path(data_path("evalsets/contract_traps_03.txt")).read_text(encoding="utf-8")
    st.session_state["last_task"] = run_check(
        user=user, contract_text=text, repo=repo, enterprise_id="E1", save=False
    )
    st.session_state["ask_clause_review"] = False
# 注入假 LLMConfig 只证明“有模型”，不联网：run_check 不传 llm，抽取走正则。
def fake_llm(cfg, repo=None):
    return LLMConfig(api_key="test") if LLM_ON else None


with patch.object(ui_app, "_resolve_llm", fake_llm):
    ui_app._page_check(repo, user)
"""


def _check_page(tmp_path, *, with_task: bool = False, llm: bool = True, role: Role = Role.USER) -> AppTest:
    script = (
        CHECK_PAGE_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "ui.sqlite3")))
        .replace("WITH_TASK", "True" if with_task else "False")
        .replace("LLM_ON", "True" if llm else "False")
        .replace("ROLE_NAME", repr(role.value))
    )
    return AppTest.from_string(script, default_timeout=60).run()


GATE_COPY = "未配置模型，合同核对不可用；配置后才能给出结论。"
SETTINGS_LINK_LABEL = "去设置页配置 API Key"


def test_check_page_empty_state_shows_guide(tmp_path) -> None:
    """有模型且无结果时左栏出输入表单，右栏给引导文案，不把整页撑长。"""
    at = _check_page(tmp_path, with_task=False)
    assert not at.exception

    markdowns = "\n".join(m.value for m in at.markdown)
    assert "合同输入" in markdowns
    assert any(b.label == "开始核对" for b in at.button)
    texts = _all_text(at)
    assert "左侧粘贴或选择一份合同，点击开始核对，结果显示在这里。" in texts
    assert "可选补充" not in texts


def test_check_page_right_column_shows_latest_result(tmp_path) -> None:
    """有模型且有结果时右栏渲染核对结论，可补充事实只在左栏出现一次。"""
    at = _check_page(tmp_path, with_task=True)
    assert not at.exception

    markdowns = [m.value for m in at.markdown]
    assert any("核对结论" in m for m in markdowns)
    assert sum(1 for m in markdowns if "可选补充" in m) == 1
    assert "左侧粘贴或选择一份合同，点击开始核对，结果显示在这里。" not in _all_text(at)
    assert not any(h.value == "核对结果" for h in at.subheader)


def test_check_page_blocked_without_model(tmp_path) -> None:
    """无模型时门禁：给提示，不渲染输入方式 / 合同文本框 / 开始核对 / 追问面板。"""
    at = _check_page(tmp_path, llm=False)
    assert not at.exception

    texts = _all_text(at)
    assert GATE_COPY in texts
    assert "未配置模型" in "\n".join(w.value for w in at.warning)
    assert not any(b.label == "开始核对" for b in at.button)
    assert not any(t.label == "合同正文" for t in at.text_area)
    assert "合同输入" not in "\n".join(m.value for m in at.markdown)
    assert not at.form, "追问面板不应出现"
    assert any("合同核对" in h.value for h in at.header), "门禁仍保留页面标题"
    assert "请联系管理员在设置页配置模型。" in "\n".join(c.value for c in at.caption)
    assert not any(p.value == SETTINGS_LINK_LABEL for p in at.page_link), "普通用户不该看到点不动的设置入口"


def test_check_page_blocked_without_model_admin_sees_settings_link(tmp_path) -> None:
    """无模型 + 管理员：给出去设置页配置 API Key 的链接。"""
    at = _check_page(tmp_path, llm=False, role=Role.ADMIN)
    assert not at.exception

    assert GATE_COPY in _all_text(at)
    assert any(p.value == SETTINGS_LINK_LABEL for p in at.page_link)
    assert not any("请联系管理员" in c.value for c in at.caption)


def test_check_page_allows_with_model(tmp_path) -> None:
    """有模型时行为不变：出现开始核对，不出现门禁提示。"""
    at = _check_page(tmp_path, llm=True)
    assert not at.exception

    assert any(b.label == "开始核对" for b in at.button)
    assert GATE_COPY not in _all_text(at)
    assert not any(p.value == SETTINGS_LINK_LABEL for p in at.page_link)


def test_result_render_alone_has_no_optional_facts(tmp_path) -> None:
    """结果区单独渲染（历史回看）不再带可补充事实，避免重复出现。"""
    script = RESULT_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "ui.sqlite3")))
    at = AppTest.from_string(script, default_timeout=60).run()
    assert not at.exception
    assert "可选补充" not in _all_text(at)


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


SPLIT_SCRIPT = """
from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.domain.models import CheckTask, RuleCoverage, TaskMode, UndeterminedRule, UnmetTerm
from mla.ui.app import _render_task

repo = SqliteRepository(
    paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
    db_path=DB_PATH,
)
task = CheckTask(
    task_id="T-SPLIT",
    mode=TaskMode.CHECK,
    enterprise_id="E1",
    undetermined=[
        UndeterminedRule(
            rule_id="R-SI-REGISTER-30D",
            name="应自用工之日起 30 日内办理社会保险登记",
            statute_id="SIL-58",
            missing_fields=("days_to_si_register",),
            reason="field_missing",
        )
    ],
    unmet=[
        UnmetTerm(
            rule_id="R-WPR-MONTHLY-PAY",
            name="工资至少每月支付一次",
            statute_id="WPR-7",
            fields=("pay_times_per_month",),
        )
    ],
    statutory_na=8,
    rule_coverage=RuleCoverage(total=20, violated=0, compliant=3, not_applicable=10, unknown=1),
)
_render_task(task, repo)
"""


def test_result_splits_unmet_statutory_and_missing_facts(tmp_path) -> None:
    """三个区块各出现一次、互不重复：合同未约定 / 需补充事实 / 法定兜底汇总。"""
    script = SPLIT_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "ui.sqlite3")))
    at = AppTest.from_string(script, default_timeout=60).run()
    assert not at.exception

    markdowns = [m.value for m in at.markdown]
    assert markdowns.count("**合同未约定**") == 1
    assert markdowns.count("**需补充事实**") == 1
    texts = _all_text(at)
    assert "未约定每月发薪次数，无法判断是否满足“工资至少每月支付一次”" in texts
    assert "《工资支付暂行规定》第七条" in texts
    assert "社保登记超期天数" in texts
    assert "另有 8 条规则因合同未约定而适用法定标准。" in texts
    assert "未能判定的规则" not in texts
    # 互斥：未约定项不进「需补充事实」，合同外事实不进「合同未约定」
    assert texts.count("每月发薪次数") == 1
    assert texts.count("社保登记超期天数") == 1


def test_result_top_admits_incomplete_coverage(tmp_path) -> None:
    """没有条目但审查没跑：顶部必须写“覆盖不完整”，不能读成没问题。"""
    script = SPLIT_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "ui.sqlite3")))
    at = AppTest.from_string(script, default_timeout=60).run()
    assert not at.exception

    texts = _all_text(at)
    assert "未发现硬性风险，但覆盖不完整" in texts
    assert "条款审查未运行" in texts
    assert "1 条规则因缺少事实无法判定" in texts
    # 结论句只在顶部说一次：结论区不再复述“未发现硬性风险项”这类判断。
    assert "未发现可判定的硬性风险项" not in texts
    assert "本次未发现硬性风险项" not in texts


HISTORY_LIST_SCRIPT = """
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
text = Path(data_path("evalsets/contract_traps_01.txt")).read_text(encoding="utf-8")
run_check(
    user=user,
    contract_text=text,
    repo=repo,
    enterprise_id="E1",
    contract_name="contract_traps_01.txt",
)
ui_app._page_history(repo, user)
"""


def test_history_row_marks_incomplete_coverage(tmp_path) -> None:
    """历史列表用结论条数 + 覆盖状态，不再只写“N 条风险”。"""
    script = HISTORY_LIST_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "ui.sqlite3")))
    at = AppTest.from_string(script, default_timeout=60).run()
    assert not at.exception

    texts = _all_text(at)
    assert "0 条风险 · 覆盖不完整" in texts, "没条目但覆盖不全，历史行必须标注"
    assert "0 条风险 · 0 条建议" not in texts, "历史行不再省略覆盖状态"


def test_history_row_hides_eval_dataset_name(tmp_path) -> None:
    """旧数据里存着评测集文件名：历史列表要显示“示例合同”，不能露 contract_traps。"""
    script = HISTORY_LIST_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "ui.sqlite3")))
    at = AppTest.from_string(script, default_timeout=60).run()
    assert not at.exception

    texts = _all_text(at)
    assert "示例合同" in texts
    assert "contract_traps" not in texts


def test_history_row_badge_uses_traceability_copy(tmp_path) -> None:
    """历史行徽标与结果页同一套说法：只说引用能不能回链，不写“依据通过”。"""
    script = HISTORY_LIST_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "ui.sqlite3")))
    at = AppTest.from_string(script, default_timeout=60).run()
    assert not at.exception

    texts = _all_text(at)
    assert "引用可溯源" in texts
    assert "依据通过" not in texts
    assert "依据校验通过" not in texts


CONTRACTS_SCRIPT = """
import streamlit as st
from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.domain.models import ContractRecord, Role, User
import mla.ui.app as ui_app

repo = SqliteRepository(
    paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
    db_path=DB_PATH,
)
user = User(
    user_id="U1", username="ui", password_hash="x", salt="y", role=Role.USER, enterprise_id="E1"
)
st.session_state["user"] = user
repo.save_contract(
    ContractRecord(
        contract_id="C1",
        enterprise_id="E1",
        filename="contract_traps_01.txt",
        text="甲方聘用乙方，月工资 5000 元。" * 30,
    )
)
ui_app._page_contracts(repo, user)
"""


def test_contracts_page_hides_eval_dataset_name(tmp_path) -> None:
    """合同管理列表同样只显示清洗后的名字（存储值不动，只改展示）。"""
    script = CONTRACTS_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "ui.sqlite3")))
    at = AppTest.from_string(script, default_timeout=60).run()
    assert not at.exception

    texts = _all_text(at)
    assert "示例合同" in texts
    assert "contract_traps" not in texts


DELETE_CONTRACT_SCRIPT = """
import streamlit as st
from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.domain.models import ContractRecord, Role, User
import mla.ui.app as ui_app

repo = SqliteRepository(
    paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
    db_path=DB_PATH,
)
user = User(
    user_id="U1", username="ui", password_hash="x", salt="y", role=Role.USER, enterprise_id="E1"
)
st.session_state["user"] = user
repo.save_contract(
    ContractRecord(
        contract_id="C-ABC12345",
        enterprise_id="E1",
        filename="contract_traps_01.txt",
        text="甲方聘用乙方，月工资 5000 元。" * 30,
    )
)
st.session_state["pending_contract_delete"] = PENDING_ID
ui_app._page_contracts(repo, user)
"""


@pytest.mark.parametrize(
    ("pending", "expected"),
    [("C-ABC12345", "示例合同"), ("C-MISSING", "该合同")],
)
def test_delete_confirm_prompt_hides_contract_id(tmp_path, pending: str, expected: str) -> None:
    """删除确认框只说合同名：查得到用清洗名，查不到用“该合同”，都不回显内部 ID。"""
    script = DELETE_CONTRACT_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "ui.sqlite3"))).replace(
        "PENDING_ID", repr(pending)
    )
    at = AppTest.from_string(script, default_timeout=60).run()
    assert not at.exception

    prompts = [w.value for w in at.warning if "确认删除" in w.value]
    assert len(prompts) == 1, prompts
    assert expected in prompts[0], prompts[0]
    assert pending not in prompts[0], f"确认框回显了内部 ID：{prompts[0]}"
    assert "contract_traps" not in prompts[0], prompts[0]


DELETE_TASK_SCRIPT = """
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
text = Path(data_path("evalsets/contract_traps_01.txt")).read_text(encoding="utf-8")
task = run_check(
    user=user,
    contract_text=text,
    repo=repo,
    enterprise_id="E1",
    contract_name="contract_traps_01.txt",
)
st.session_state["pending_delete"] = PENDING_ID
ui_app._page_history(repo, user)
"""


@pytest.mark.parametrize(
    ("pending_expr", "expected"),
    [("task.task_id", "示例合同"), ('"T-MISSING"', "该核对记录")],
)
def test_delete_record_prompt_hides_task_id(tmp_path, pending_expr: str, expected: str) -> None:
    """删除记录的确认框只说“合同名 + 时间”：查得到用清洗名，查不到用“该核对记录”。"""
    script = DELETE_TASK_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "ui.sqlite3"))).replace(
        "PENDING_ID", pending_expr
    )
    at = AppTest.from_string(script, default_timeout=60).run()
    assert not at.exception

    prompts = [w.value for w in at.warning if "确认删除" in w.value]
    assert len(prompts) == 1, prompts
    assert expected in prompts[0], prompts[0]
    assert "T-" not in prompts[0], f"确认框回显了内部 ID：{prompts[0]}"
    assert "contract_traps" not in prompts[0], prompts[0]
    if expected == "示例合同":
        assert re.search(r"\d{4}-\d{2}-\d{2}", prompts[0]), f"提示缺少时间：{prompts[0]}"


COLLECT_INPUT_SCRIPT = """
import streamlit as st
from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.domain.models import ContractRecord, Role, User
import mla.ui.app as ui_app

repo = SqliteRepository(
    paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
    db_path=DB_PATH,
)
user = User(
    user_id="U1", username="ui", password_hash="x", salt="y", role=Role.USER, enterprise_id="E1"
)
st.session_state["user"] = user
repo.save_contract(
    ContractRecord(
        contract_id="C1",
        enterprise_id="E1",
        filename="contract_traps_01.txt",
        text="甲方聘用乙方，月工资 5000 元。" * 30,
    )
)
ui_app._collect_input(repo, user)
"""


def test_history_select_label_hides_eval_dataset_name(tmp_path) -> None:
    """“从历史合同选择”的选项标签同样显示清洗后的名字（选项仍是合同对象）。"""
    script = COLLECT_INPUT_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "ui.sqlite3")))
    at = AppTest.from_string(script, default_timeout=60).run()
    assert not at.exception
    at.segmented_control[0].set_value("从历史合同选择").run()
    assert not at.exception

    options = [opt for box in at.selectbox for opt in box.options]
    assert any("示例合同" in opt for opt in options), options
    assert not any("contract_traps" in opt for opt in options), options


def test_check_page_offers_contract_qa(tmp_path) -> None:
    """核对结果底部有可折叠的提问区；提问后渲染结论与依据，上下文不带合同正文。"""
    script = QA_PANEL_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "ui.sqlite3")))
    at = AppTest.from_string(script, default_timeout=60).run()
    assert not at.exception

    labels = [e.label for e in at.get("expander")]
    assert "就本合同继续提问" in labels
    assert any(x.label == "问题" and "不发送合同正文" in (x.help or "") for x in at.text_area), (
        "提问框的 help 必须说明不发送合同正文"
    )

    next(x for x in at.text_area if x.label == "问题").set_value("试用期约定有什么风险？")
    next(b for b in at.button if b.label == "提问").click().run()
    assert not at.exception

    context = at.session_state["captured_context"]
    assert set(context) == {"facts", "items", "clauses"}
    assert "contract_text" not in context
    assert "结论与依据（法条原文）" in _all_text(at)
    assert any("试用期最长不得超过六个月。" in m.value for m in at.markdown)
    assert _disclaimer_count(at) == 1, "问答结论同样只留底部一行免责"
