"""Streamlit 界面：登录 → 合同核对（可就本次结果继续提问）→ 企业管理 → 历史 → 管理端。

设计取法（见技能 references/design.md / layouts.md）：
- 颜色、字体、圆角全部走 `.streamlit/config.toml`，不写 CSS；
- 图标统一用 Material Symbols（`:material/xxx:`），不用 emoji；
- 卡片用 `st.container(border=True)`；指标用 `st.metric`；依据用 `st.badge`；
- 三区物理分开：核对结论 / 处理建议 / 类案参照。

启动：`.venv\\Scripts\\python -m streamlit run src/mla/ui/app.py`
"""

from __future__ import annotations

import csv
import io
import json
import os
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import altair as alt
import streamlit as st

from mla.adapters.files import parse_contract_bytes
from mla.adapters.llm import DeepSeekLLM
from mla.adapters.persist import SqliteRepository
from mla.config import LLMConfig, data_path, load_config
from mla.domain.asking import candidate_fields
from mla.domain.field_catalog import field_hint, field_name, field_question
from mla.domain.models import (
    MISSING_TERM_BASE,
    MISSING_TERM_UNJUDGED_ID,
    ContractRecord,
    Enterprise,
    MissingStatus,
    Role,
    TaskMode,
    User,
)
from mla.domain.policy import ACCEPTANCE, MAX_ASK_CANDIDATES
from mla.domain.slots import DEFAULT_SLOTS, derive_slots
from mla.services.asking import suggest_optional
from mla.services.auth import (
    MIN_PASSWORD_LEN,
    authenticate,
    change_password,
    create_first_admin,
    delete_account,
    has_admin,
    is_admin,
    register,
    require_admin,
)
from mla.services.report import build_report_docx, merge_findings
from mla.services.tasks import persist, run_check, run_qa
from mla.ui.trend import trend_frame

st.set_page_config(
    page_title="用工风险辅助 · 小微企业",
    page_icon=":material/verified_user:",
    layout="wide",
)

DISCLAIMER_FALLBACK = "非法律意见，仅供参考；重大争议请咨询专业人士。"
CASE_NOTE = "同类情形通常如何认定，不构成本案结论。"
# 条款审查（存在性域）：等级徽标、片段截断与免责文案集中在此，结果页与历史详情共用。
# 等级文案只说“与法条存在冲突”，不判“违法/无效”——与 RESULT_DISCLAIMER 的免责口径一致，
# 也与 prompt 里“不要写该条款违法/无效”的硬性要求对齐。
CLAUSE_GRADES: tuple[tuple[str, str, str], ...] = (
    ("A", "A 与法条明文冲突", "red"),
    ("B", "B 显著失衡", "orange"),
    ("C", "C 待确认", "gray"),
)
CLAUSE_SPAN_LIMIT = 80
# 合并展示的来源徽标：档位 0 = 规则确定性判定，1/2/3 = 条款审查的 A/B/C 级；
# 排序口径与 Word 报告共用 `merge_findings`，避免界面与报告两处漂移。
FINDING_SOURCE_BADGES: tuple[str, ...] = (
    "规则判定·确定性",
    "模型审查·A",
    "模型审查·B",
    "模型审查·C",
)
FINDING_SOURCE_COLORS: tuple[str, ...] = ("blue", "red", "orange", "gray")
CLAUSE_NOT_REVIEWED_NOTE = "本次未做条款级审查，条款级风险未覆盖。"
CLAUSE_CLEAR_NOTE = "本次条款级审查未发现可疑条款。"
# 出现这些诊断说明审查没跑起来或整批输出不可用，只能说“未做”，不能说“未发现”（见 _clause_review_note）。
_NOT_REVIEWED_MARKERS: tuple[str, ...] = ("未启用", "失败", "已忽略")
# 结果页底部常驻免责：核对与问答共用一句，不依赖本次是否启用条款审查，也不判“违法/无效”。
RESULT_DISCLAIMER = "以上为风险提示与依据定位，不构成法律意见；条款效力以仲裁机构或法院裁判为准。"
# 必备条款汇总句末尾的分母说明：KPI 用 6/8，汇总句合计 9 类，不点明容易被读成矛盾。
MISSING_TERMS_BASE_NOTE = "（第（九）类为兜底条款，不参与覆盖率）"
LOGO = Path(__file__).parent / "assets" / "logo.svg"


@st.cache_resource
def get_repo(db_path: str) -> SqliteRepository:
    return SqliteRepository(db_path=db_path)


@st.cache_resource
def get_config():
    return load_config()


# 模型 Key 的三处来源，顺序即优先级。三处都读原始值，不经过 load_config：
# load_config 会把环境变量与 secrets 合成一个 api_key，混起来就分不清谁盖过谁。
LLM_KEY_SETTING = "llm_api_key"
KEY_SOURCE_LABELS: tuple[str, ...] = ("secrets.toml", "数据库设置", "环境变量")


def _secrets_llm_key() -> str:
    """只取 `.streamlit/secrets.toml` 里 [llm].api_key 这一处原始值。"""
    try:
        section = st.secrets.get("llm", {})
    except Exception:  # noqa: BLE001 - 没有 secrets 文件时按未配置处理
        return ""
    return str(section.get("api_key", "")) if hasattr(section, "get") else ""


def llm_key_sources(repo: SqliteRepository | None = None) -> tuple[str, str, str]:
    """三处原始 Key，顺序即优先级：secrets.toml / 数据库设置 / 环境变量。"""
    db_key = repo.get_setting(LLM_KEY_SETTING) if repo is not None else ""
    return (_secrets_llm_key(), db_key, os.environ.get("MLA_LLM_API_KEY", ""))


def resolve_llm_key(sources: tuple[str, ...]) -> str:
    """按优先级取第一个非空值；都为空返回空串（界面按未配置处理）。"""
    return next((str(value) for value in sources if value), "")


def key_source_label(sources: tuple[str, ...]) -> str:
    """当前生效的 Key 来自哪一处；都没有就是未配置。"""
    for label, value in zip(KEY_SOURCE_LABELS, sources, strict=False):
        if value:
            return label
    return "未配置"


def _default_llm_key(repo: SqliteRepository | None = None) -> str:
    return resolve_llm_key(llm_key_sources(repo))


def _resolve_llm(cfg, repo: SqliteRepository | None = None) -> LLMConfig | None:
    """只认三处来源（secrets.toml / 数据库 / 环境变量），不读任何会话级 Key。

    调用点必须传 repo：否则数据库里保存的 Key 读不到，界面会误判“未配置模型”。
    """
    key = _default_llm_key(repo)
    if not key:
        return None
    return replace(cfg.llm, api_key=key)


def _trend_chart(frame) -> alt.Chart:
    """分组柱状图：横轴显示时间标签，排序仍用数值次序（旧记录只有日期）。"""
    order = frame["标签"].tolist()
    long = frame.melt(
        id_vars=["次序", "标签", "日期"],
        value_vars=["风险条目", "处理建议"],
        var_name="指标",
        value_name="条数",
    )
    return (
        alt.Chart(long)
        .mark_bar(cornerRadius=3, size=26)
        .encode(
            x=alt.X(
                "标签:N",
                title=None,
                sort=order,
                scale=alt.Scale(paddingInner=0.3, paddingOuter=0.12),
                axis=alt.Axis(labelAngle=-40, labelFontSize=12, labelLimit=120),
            ),
            xOffset=alt.XOffset("指标:N", sort=["风险条目", "处理建议"]),
            y=alt.Y("条数:Q", title=None, axis=alt.Axis(format="d", tickMinStep=1, labelFontSize=13)),
            color=alt.Color(
                "指标:N",
                scale=alt.Scale(domain=["风险条目", "处理建议"], range=["#1E40AF", "#0F766E"]),
                legend=alt.Legend(orient="top", title=None, labelFontSize=13),
            ),
            tooltip=["日期", "指标", "条数"],
        )
        .properties(height=260)
        .configure_view(strokeWidth=0)
        .configure_axis(gridColor="#E2E8F0")
    )


def source_label(repo: SqliteRepository, source_id: str) -> str:
    """LCL-19 → 《劳动合同法》第十九条（查不到就原样显示）。"""
    statute = repo.get_statute(source_id)
    if statute is None:
        return source_id
    short = statute.law_name.replace("中华人民共和国", "")
    return f"《{short}》{statute.article_no}"


def _statute_meta(statute) -> str:
    """法条元信息一行：施行日期 + 来源链接；内部 ID 不对用户显示。"""
    meta = f"施行日期：{statute.effective_date or '未记录'}"
    if statute.source_url:
        meta += f" · 来源：{statute.source_url}"
    return meta


def _facts_from_enterprise(repo: SqliteRepository, user: User) -> dict[str, str]:
    """企业档案里已录的 3 个事实直接预填，避免重复追问（降误问率）。"""
    if not user.enterprise_id:
        return {}
    ent = repo.get_enterprise(user.enterprise_id)
    if ent is None:
        return {}
    facts: dict[str, str] = {}
    if ent.employer_type:
        facts["employer_type"] = ent.employer_type
    if ent.working_hours:
        facts["working_hours"] = ent.working_hours
    if ent.social_insurance in ("是", "否"):
        facts["social_insurance"] = ent.social_insurance
    return facts


QUESTION_TO_SLOT = {s.question: s.slot_id for s in DEFAULT_SLOTS}

# 企业档案录入、但不参与规则判定的两项：没有槽位，界面仍要显示中文名而不是原始 id
ARCHIVE_FIELD_NAMES: dict[str, str] = {
    "working_hours": "工时制度",
    "social_insurance": "社保缴纳",
}


def fact_display_name(field_id: str) -> str:
    """事实字段显示名：先查档案专用名，再走字段目录，都没命中就原样显示 id。"""
    return ARCHIVE_FIELD_NAMES.get(field_id) or field_name(field_id)


SLOT_HINTS = {
    "employer_type": "例如：有限责任公司 / 个体工商户 / 个人独资企业",
    "written_contract": "填 是 或 否",
    "term_years": "例如：3（无固定期限填 0）",
    "probation_months": "例如：3",
    "working_hours": "例如：标准工时 / 综合计算工时 / 不定时工时",
    "social_insurance": "填 是 或 否",
    "wage": "例如：基本工资 4000 + 绩效 1000",
}


def page_header(icon: str, title: str) -> None:
    """页面标题只写标题：分页名、导航与空态已经说明这一页做什么，不再加副标题。"""
    st.header(title, icon=f":material/{icon}:", anchor=False)
    st.space("small")


# ---------------- 侧栏与导航 ----------------
NAV: dict[str, st.Page] = {}


def _sidebar_chrome() -> None:
    if LOGO.exists():
        st.logo(str(LOGO), size="large")


def _sidebar_footer(user: User) -> None:
    st.sidebar.space("small")
    st.sidebar.caption(f"{user.username} · {user.role}")
    if st.sidebar.button("退出登录", width="stretch", icon=":material/logout:"):
        for k in (
            "user",
            "last_task",
            "last_qa_context",
            "ask_pending",
            "ask_contract",
            "ask_clause_review",
        ):
            st.session_state.pop(k, None)
        st.rerun()


def _build_nav(repo: SqliteRepository, user: User) -> st.Page:
    """真实多页导航（st.navigation）：分组、图标、URL 都由 Streamlit 原生渲染。

    工作台只有合同核对且为默认页；企业档案 / 合同管理 / 历史记录收进管理组；
    管理端独立成组，仅管理员可见。
    """
    NAV.clear()
    NAV["合同核对"] = st.Page(
        lambda: _page_check(repo, user),
        title="合同核对",
        icon=":material/fact_check:",
        url_path="check",
        default=True,
    )
    NAV["企业档案"] = st.Page(
        lambda: _page_enterprise(repo, user),
        title="企业档案",
        icon=":material/domain:",
        url_path="enterprise",
    )
    NAV["合同管理"] = st.Page(
        lambda: _page_contracts(repo, user),
        title="合同管理",
        icon=":material/folder_open:",
        url_path="contracts",
    )
    NAV["历史记录"] = st.Page(
        lambda: _page_history(repo, user),
        title="历史记录",
        icon=":material/history:",
        url_path="history",
    )
    NAV["设置"] = st.Page(
        lambda: _page_settings(repo, user),
        title="设置",
        icon=":material/settings:",
        url_path="settings",
    )
    sections: dict[str, list[st.Page]] = {
        "工作台": [NAV["合同核对"]],
        "管理": [NAV["企业档案"], NAV["合同管理"], NAV["历史记录"], NAV["设置"]],
    }
    if is_admin(user):
        NAV["管理端"] = st.Page(
            lambda: _page_admin(repo, user),
            title="管理端",
            icon=":material/admin_panel_settings:",
            url_path="admin",
        )
        sections["管理端"] = [NAV["管理端"]]
    return st.navigation(sections)


# ---------------- 登录 ----------------
# 登录页只留图标 + 标题 + 登录/注册：用途、边界、数据去向都在首次登录的引导弹窗里说一次。
FORGOT_PASSWORD_HEAD = "本地应用无法通过邮箱找回。请在装有本程序的机器上打开终端执行："
FORGOT_PASSWORD_TAIL = "会生成一个新口令并显示一次。若所有管理员账号都忘记了，执行："
FORGOT_PASSWORD_RESET_CMD = "python -m mla admin reset --username 你的账号"
FORGOT_PASSWORD_LIST_CMD = "python -m mla admin list"
FORGOT_PASSWORD_PROMOTE_CMD = "python -m mla admin promote --username 你的账号"
INIT_PASSWORD_REMINDER = "请记住管理员账号与口令。忘记后只能在本机终端重置：python -m mla admin reset"


def _forgot_password_help() -> None:
    """忘记密码说明：只给可复制的命令，不解释实现原理。"""
    st.caption(FORGOT_PASSWORD_HEAD)
    st.code(FORGOT_PASSWORD_RESET_CMD, language="bash")
    st.caption(FORGOT_PASSWORD_TAIL)
    st.code(f"{FORGOT_PASSWORD_LIST_CMD}\n{FORGOT_PASSWORD_PROMOTE_CMD}", language="bash")


def _login_screen(repo: SqliteRepository) -> None:
    st.space("large")
    left, right = st.columns([1.15, 1], gap="large", vertical_alignment="center")
    with left:
        if LOGO.exists():
            st.image(str(LOGO), width=56)
        st.title("小微企业用工风险辅助系统", anchor=False)
    with right:
        with st.container(border=True):
            tab_login, tab_reg = st.tabs([":material/login: 登录", ":material/person_add: 注册"])
            with tab_login:
                with st.form("login"):
                    name = st.text_input("用户名", placeholder="请输入用户名")
                    pwd = st.text_input("密码", type="password", placeholder="请输入密码")
                    ok = st.form_submit_button("登录", type="primary", width="stretch")
                if ok:
                    user = authenticate(repo, name, pwd)
                    if user is None:
                        st.error("用户名或密码不正确", icon=":material/error:")
                    else:
                        st.session_state["user"] = user
                        st.rerun()
                if st.button("忘记密码", type="tertiary", key="forgot_pwd"):
                    st.session_state["forgot_pwd_open"] = not st.session_state.get("forgot_pwd_open", False)
                if st.session_state.get("forgot_pwd_open"):
                    _forgot_password_help()
            with tab_reg:
                with st.form("register"):
                    name2 = st.text_input("用户名（≥3 字符）")
                    pwd2 = st.text_input(f"密码（≥{MIN_PASSWORD_LEN} 位）", type="password")
                    ent_name = st.text_input("企业名称（可选）")
                    ok2 = st.form_submit_button("注册", width="stretch")
                if ok2:
                    try:
                        ent_id = ""
                        if ent_name.strip():
                            ent_id = "E-" + uuid.uuid4().hex[:8]
                            repo.save_enterprise(Enterprise(enterprise_id=ent_id, name=ent_name.strip()))
                        user = register(repo, name2, pwd2, Role.USER, ent_id)
                        st.session_state["user"] = user
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc), icon=":material/error:")


# ---------------- 首次初始化（库里还没有管理员） ----------------
def init_form_error(repo: SqliteRepository, username: str, password: str, confirm: str) -> str:
    """初始化表单校验：返回第一条错误，空串表示可以建号。

    顺序固定：账号非空 → 两次密码一致 → 长度 → 重名；先查重名再建企业，避免留下空企业。
    """
    name = username.strip()
    if not name:
        return "请填写管理员账号"
    if password != confirm:
        return "两次输入的密码不一致"
    if len(password) < MIN_PASSWORD_LEN:
        return f"密码至少 {MIN_PASSWORD_LEN} 位"
    if repo.get_user_by_name(name):
        return f"用户名已存在：{name}"
    return ""


def _init_screen(repo: SqliteRepository) -> None:
    """空库首次运行：先建管理员，建完直接进主界面，不用再登录一次。"""
    st.space("large")
    left, right = st.columns([1.15, 1], gap="large", vertical_alignment="center")
    with left:
        if LOGO.exists():
            st.image(str(LOGO), width=56)
        st.title("首次使用", anchor=False)
    with right:
        with st.container(border=True):
            with st.form("init_admin"):
                username = st.text_input("管理员账号", placeholder="请输入管理员账号")
                pwd = st.text_input("登录密码", type="password", placeholder=f"至少 {MIN_PASSWORD_LEN} 位")
                pwd2 = st.text_input("确认密码", type="password", placeholder="再输入一次")
                ent_name = st.text_input("企业名称", placeholder="可留空，稍后在企业档案里补")
                created = st.form_submit_button("创建并进入", type="primary", width="stretch")
            st.caption(INIT_PASSWORD_REMINDER)
            if created:
                error = init_form_error(repo, username, pwd, pwd2)
                if error:
                    st.error(error, icon=":material/error:")
                else:
                    try:
                        user = create_first_admin(repo, username, pwd, ent_name)
                    except ValueError as exc:
                        st.error(str(exc), icon=":material/error:")
                    else:
                        st.session_state["user"] = user
                        st.rerun()


# ---------------- 首次登录引导（每个账号一次） ----------------
ONBOARDING_TITLE = "开始之前"
# 登录页删掉的说明集中在这里说一次：用途、边界、数据去向、范围。
ONBOARDING_POINTS: tuple[str, ...] = (
    "用途：核对劳动合同中的用工风险，每条结论都给出法条依据。",
    "边界：只提示与法条存在冲突的地方，不判定违法或无效，不构成法律意见。",
    "数据：核对时合同正文会发送给模型服务用于抽取；检索、追问与问答只发送结构化事实。",
    "范围：只处理劳动用工与劳动合同问题，其他问题会拒答并建议咨询专业人士。",
)


def _should_onboard(user: User | None) -> bool:
    """是否要弹首次引导：只看落库的 onboarded 字段，不看会话状态（换设备也不重弹）。"""
    return user is not None and not user.onboarded


def _onboarding_content() -> tuple[str, tuple[str, ...]]:
    """引导弹窗的标题与正文条目，单独抽出来便于测试文案本身。"""
    return ONBOARDING_TITLE, ONBOARDING_POINTS


@st.dialog(ONBOARDING_TITLE, icon=":material/info:", on_dismiss="rerun")
def _onboarding_dialog(repo: SqliteRepository, user: User) -> None:
    """模态弹窗：确认后才算引导完成；点关闭等同未确认，下次登录仍会弹。"""
    _title, points = _onboarding_content()
    st.markdown("\n".join(f"- {point}" for point in points))
    if st.button("我知道了", type="primary", width="stretch", icon=":material/check:"):
        repo.mark_onboarded(user.user_id)
        st.session_state["user"] = replace(user, onboarded=True)
        st.rerun()


# ---------------- 企业 ----------------
ENTERPRISE_KINDS: tuple[str, ...] = ("有限责任公司", "个体工商户", "个人独资企业")
ENTERPRISE_HOURS: tuple[str, ...] = ("标准工时", "综合计算工时", "不定时工时")
ENTERPRISE_SI: tuple[str, ...] = ("是", "否")
# 三项必选：没选就追问，不能靠默认值蒙过去
ENTERPRISE_REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
    ("employer_type", "用工主体类型"),
    ("working_hours", "工时制度"),
    ("social_insurance", "是否缴纳社保"),
)


def enterprise_profile_errors(values: dict[str, str | None]) -> tuple[str, ...]:
    """档案必选项校验：返回没选的项目名，空元组表示可以保存。"""
    return tuple(label for key, label in ENTERPRISE_REQUIRED_FIELDS if not str(values.get(key) or "").strip())


def _page_enterprise(repo: SqliteRepository, user: User) -> None:
    page_header("domain", "企业档案")
    ent = repo.get_enterprise(user.enterprise_id) if user.enterprise_id else None
    with st.container(border=True):
        with st.form("ent"):
            c1, c2 = st.columns(2)
            with c1:
                name = st.text_input(
                    "企业名称", value=ent.name if ent else "", placeholder="例如：某某科技有限公司"
                )
                kind = st.selectbox(
                    "用工主体类型",
                    ENTERPRISE_KINDS,
                    index=ENTERPRISE_KINDS.index(ent.employer_type)
                    if ent and ent.employer_type in ENTERPRISE_KINDS
                    else None,
                    placeholder="请选择用工主体类型",
                )
                size = st.number_input("用工人数", min_value=0, value=int(ent.staff_size) if ent else 0)
            with c2:
                hours = st.selectbox(
                    "工时制度",
                    ENTERPRISE_HOURS,
                    index=ENTERPRISE_HOURS.index(ent.working_hours)
                    if ent and ent.working_hours in ENTERPRISE_HOURS
                    else None,
                    placeholder="请选择工时制度",
                )
                si = st.selectbox(
                    "是否缴纳社保",
                    ENTERPRISE_SI,
                    index=ENTERPRISE_SI.index(ent.social_insurance)
                    if ent and ent.social_insurance in ENTERPRISE_SI
                    else None,
                    placeholder="请选择是否缴纳社保",
                )
            saved = st.form_submit_button("保存档案", type="primary", icon=":material/save:")
    if saved:
        missing = enterprise_profile_errors(
            {"employer_type": kind, "working_hours": hours, "social_insurance": si}
        )
        if missing:
            st.error("保存前请先选择：" + "、".join(missing), icon=":material/error:")
        else:
            ent_id = user.enterprise_id or "E-" + uuid.uuid4().hex[:8]
            repo.save_enterprise(
                Enterprise(
                    enterprise_id=ent_id,
                    name=name,
                    employer_type=kind or "",
                    staff_size=int(size),
                    working_hours=hours or "",
                    social_insurance=si or "",
                )
            )
            if not user.enterprise_id:
                repo.set_user_enterprise(user.user_id, ent_id)
                st.session_state["user"] = replace(user, enterprise_id=ent_id)
                st.success("档案已保存，并已绑定到当前账号。", icon=":material/check_circle:")
                st.rerun()
            st.success(f"已保存 `{ent_id}`", icon=":material/check_circle:")

    if ent is not None:
        with st.expander("删除企业档案", icon=":material/delete:"):
            st.caption("删除后企业信息与已保存合同一并移除，核对历史保留。")
            sure = st.checkbox("我确认删除该企业档案", key="del_ent_ok")
            if st.button(
                "确认删除",
                type="primary",
                disabled=not sure,
                icon=":material/delete_forever:",
            ):
                repo.delete_enterprise(ent.enterprise_id)
                st.success("企业档案已删除。", icon=":material/check_circle:")
                st.rerun()


# ---------------- 合同管理 ----------------
def _page_contracts(repo: SqliteRepository, user: User) -> None:
    page_header("folder_open", "合同管理")
    ent_id = user.enterprise_id or ""
    if not ent_id:
        with st.container(border=True):
            st.warning("当前账号未绑定企业，合同按企业隔离。", icon=":material/domain:")
            st.page_link(NAV["企业档案"], label="去完善企业档案", icon=":material/arrow_forward:")
        return
    contracts = repo.list_contracts(ent_id)
    if not contracts:
        with st.container(border=True):
            st.info("还没有保存过合同；在“合同核对”里粘贴或上传后会自动存档。", icon=":material/folder_open:")
            st.page_link(NAV["合同核对"], label="去核对合同", icon=":material/arrow_forward:")
        return

    keyword = st.text_input("搜索（文件名或正文）", placeholder="例如：试用期 / 竞业")
    rows = [c for c in contracts if not keyword or keyword in c.filename or keyword in c.text]
    st.caption(f"共 {len(contracts)} 份 · 筛出 {len(rows)} 份")

    pending = st.session_state.get("pending_contract_delete")
    if pending:
        st.warning(f"确认删除合同 {pending}？删除后不可在“从历史合同选择”里找到。", icon=":material/delete:")
        c1, c2, _ = st.columns([1, 1, 4])
        if c1.button("确认删除", type="primary", key="confirm_contract_del"):
            repo.delete_contract(pending, ent_id)
            st.session_state.pop("pending_contract_delete", None)
            st.session_state.pop("view_contract", None)
            st.rerun()
        if c2.button("取消", key="cancel_contract_del"):
            st.session_state.pop("pending_contract_delete", None)
            st.rerun()

    for contract in rows[:50]:
        with st.container(border=True):
            left, right = st.columns([4, 1.4], vertical_alignment="center")
            with left:
                st.markdown(f"**{contract.filename}**")
                st.caption(f"{len(contract.text)} 字 · {contract.uploaded_at}")
            with right:
                with st.container(horizontal=True):
                    if st.button("查看", key=f"viewc_{contract.contract_id}", icon=":material/visibility:"):
                        st.session_state["view_contract"] = contract.contract_id
                        st.rerun()
                    if st.button("删除", key=f"delc_{contract.contract_id}", icon=":material/delete:"):
                        st.session_state["pending_contract_delete"] = contract.contract_id
                        st.rerun()

    view_id = st.session_state.get("view_contract")
    contract = next((c for c in contracts if c.contract_id == view_id), None)
    if contract is not None:
        st.subheader(contract.filename, divider="gray", anchor=False)
        st.text_area("合同原文", value=contract.text, height=280, disabled=True, key="contract_view")
        if st.button("收起原文", icon=":material/keyboard_arrow_up:"):
            st.session_state.pop("view_contract", None)
            st.rerun()


# ---------------- 合同核对 ----------------
def _collect_input(repo: SqliteRepository, user: User) -> tuple[str, str, str]:
    """返回（合同正文, 合同名, 合同 ID）；合同名用于历史列表自解释。"""
    contracts = repo.list_contracts(user.enterprise_id) if user.enterprise_id else []
    source = st.segmented_control(
        "输入方式",
        ["粘贴文本", "上传文件", "从历史合同选择"],
        default="粘贴文本",
        label_visibility="collapsed",
    )
    if source == "上传文件":
        up = st.file_uploader("支持 .txt / .docx / 文本型 .pdf", type=["txt", "docx", "pdf"])
        if up is None:
            return "", "", ""
        try:
            return parse_contract_bytes(up.name, up.read()), Path(up.name).stem, ""
        except ImportError:
            st.warning(
                "解析 PDF 需要 pypdf，当前环境未安装；可先改用 .docx 或粘贴文本。",
                icon=":material/warning:",
            )
            return "", "", ""
        except Exception:  # noqa: BLE001
            st.error("文件解析失败，请改用“粘贴文本”。", icon=":material/error:")
            return "", "", ""
    if source == "从历史合同选择":
        if not contracts:
            st.info("还没有保存过合同。", icon=":material/info:")
            return "", "", ""
        labels = {f"{c.filename}（{len(c.text)} 字）": c for c in contracts}
        pick = st.selectbox("选择合同", ["（不选）", *labels.keys()])
        if pick == "（不选）":
            return "", "", ""
        chosen = labels[pick]
        with st.expander("查看原文 / 删除这份合同", icon=":material/description:"):
            st.text_area(
                "合同原文", value=chosen.text, height=200, disabled=True, key=f"view_{chosen.contract_id}"
            )
            if st.button("删除这份合同", icon=":material/delete:", key=f"del_{chosen.contract_id}"):
                repo.delete_contract(chosen.contract_id, chosen.enterprise_id)
                st.rerun()
        return chosen.text, chosen.filename, chosen.contract_id
    return (
        st.text_area(
            "合同正文",
            height=160,
            label_visibility="collapsed",
            placeholder="把劳动合同正文粘贴到这里……",
        ),
        "粘贴合同",
        "",
    )


def _page_check(repo: SqliteRepository, user: User) -> None:
    page_header("fact_check", "合同核对")
    if not user.enterprise_id:
        with st.container(border=True):
            st.warning(
                "当前账号未绑定企业。核对结果按企业归档，请先完善企业档案。",
                icon=":material/domain:",
            )
            st.page_link(NAV["企业档案"], label="去完善企业档案", icon=":material/arrow_forward:")
        return
    ent_facts = _facts_from_enterprise(repo, user)
    pending = st.session_state.get("ask_pending")
    task = st.session_state.get("last_task")

    left, right = st.columns([1, 2.2], gap="medium", vertical_alignment="top")
    with left:
        if pending:
            _ask_panel(repo, user, pending, ent_facts)
        else:
            _check_form(repo, user, ent_facts)

        if task is not None and not pending:
            _render_optional_facts(task, repo, user)

    with right:
        with st.container(border=True, height=760):
            if task is None:
                st.caption("左侧粘贴或选择一份合同，点击开始核对，结果显示在这里。")
            elif pending:
                st.caption("先在左侧补齐追问事项，答完自动重新核对，结果显示在这里。")
            else:
                _render_task(task, repo, user, clause_review=st.session_state.get("ask_clause_review"))


def _clause_review_toggle(cfg, llm: LLMConfig | None) -> bool:
    """条款审查开关：默认取配置（`MLA_CLAUSE_REVIEW`）；未配置模型时置灰并说明。"""
    default_on = bool(cfg.clause_review.enabled) and llm is not None
    st.checkbox(
        "启用条款审查（模型辅助）",
        value=default_on,
        key="clause_review_on",
        disabled=llm is None,
        help="逐条审查可疑条款并按 A/B/C 分级，约需 20–30 秒并消耗模型额度。",
    )
    if llm is None:
        return False
    return bool(st.session_state.get("clause_review_on", default_on))


def _check_form(repo: SqliteRepository, user: User, ent_facts: dict[str, str]) -> None:
    st.markdown("**合同输入**")
    text, contract_name, contract_id = _collect_input(repo, user)
    if text.strip():
        st.badge(f"将核对 {len(text)} 字", icon=":material/description:", color="blue")
    cfg = get_config()
    llm = _resolve_llm(cfg, repo)
    if llm is not None:
        st.caption(
            "核对时会把合同正文发送至模型服务（DeepSeek）用于抽取关键条款；"
            "合同可能含姓名等信息，请确认已获授权。"
        )
    clause_on = _clause_review_toggle(cfg, llm)
    if st.button("开始核对", type="primary", width="stretch", icon=":material/play_arrow:"):
        if not text.strip():
            st.warning("请先粘贴合同文本或选择一份合同。", icon=":material/warning:")
        else:
            with st.spinner("正在核对……"):
                task = run_check(
                    user=user,
                    contract_text=text,
                    facts=ent_facts,
                    repo=repo,
                    enterprise_id=user.enterprise_id,
                    max_ask_rounds=1,
                    save=False,
                    contract_name=contract_name,
                    contract_id=contract_id,
                    llm=llm,
                    clause_review=clause_on,
                )
                if user.enterprise_id and not contract_id:
                    contract_id = "C-" + uuid.uuid4().hex[:8]
                    repo.save_contract(
                        ContractRecord(
                            contract_id=contract_id,
                            enterprise_id=user.enterprise_id,
                            filename=f"{contract_name or '合同'}.txt",
                            text=text,
                        )
                    )
                st.session_state["last_task"] = task
                st.session_state["ask_contract"] = text
                st.session_state["ask_contract_name"] = contract_name
                st.session_state["ask_contract_id"] = contract_id
                st.session_state["ask_clause_review"] = clause_on
                if task.questions:
                    st.session_state["ask_pending"] = {
                        "task_id": task.task_id,
                        "questions": list(task.questions),
                        "ask_items": [dict(item) for item in task.ask_items],
                        "ask_rounds": task.ask_rounds,
                    }
            st.rerun()


def _ask_panel(repo: SqliteRepository, user: User, pending: dict, ent_facts: dict[str, str]) -> None:
    """缺阻断项时向用户追问（一轮问全）；答完重跑，答不出可跳过（降级留痕）。

    答案回填按本轮追问自带的字段 id 走：问句可能已被模型润色，按问句原文反查会写错字段；
    只有历史会话没有字段 id 时才退回问句映射。
    """
    questions = list(pending.get("questions") or [])
    ask_items = [item for item in (pending.get("ask_items") or []) if isinstance(item, dict)]
    st.markdown(f"#### 补充 {len(questions)} 项事实")
    with st.form(f"ask_{pending.get('task_id', '')}"):
        answers: dict[str, str] = {}
        for i, q in enumerate(questions):
            slot_id = str(ask_items[i].get("slot_id") or "") if i < len(ask_items) else ""
            slot_id = slot_id or QUESTION_TO_SLOT.get(q, "")
            answers[slot_id or f"未识别{i}"] = st.text_input(
                q,
                key=f"ask_{pending.get('task_id', '')}_{i}",
                placeholder=SLOT_HINTS.get(slot_id, "请填写"),
            )
        c1, c2 = st.columns(2)
        submitted = c1.form_submit_button(
            "提交并重新核对",
            type="primary",
            width="stretch",
            help="按补充的事实自动重新核对。",
        )
        skipped = c2.form_submit_button(
            "跳过，直接看结论",
            width="stretch",
            help="跳过会在结果中标注降级。",
        )

    if submitted:
        facts = dict(ent_facts)
        for slot_id, value in answers.items():
            text = str(value).strip()
            if text and not slot_id.startswith("未识别"):
                facts[slot_id] = text
        with st.spinner("按补充事实重新核对……"):
            task = run_check(
                user=user,
                contract_text=str(st.session_state.get("ask_contract") or ""),
                facts=facts,
                repo=repo,
                enterprise_id=user.enterprise_id,
                ask_rounds=int(pending.get("ask_rounds") or 0),
                max_ask_rounds=1,
                contract_name=str(st.session_state.get("ask_contract_name") or ""),
                contract_id=str(st.session_state.get("ask_contract_id") or ""),
                llm=_resolve_llm(get_config(), repo),
                clause_review=st.session_state.get("ask_clause_review"),
            )
        st.session_state["last_task"] = task
        st.session_state.pop("ask_pending", None)
        st.rerun()

    if skipped:
        task = st.session_state.get("last_task")
        if task is not None:
            persist(task, user=user, repo=repo, verify_ok=task.verify_ok)
        st.session_state.pop("ask_pending", None)
        st.rerun()


def _show_degraded_badge(task) -> bool:
    """是否显示“已降级”告警徽标。

    “未配置 Key”属预期配置状态，只给中性提示；调用失败 / 超时 / 回退才告警。
    其他历史降级原因（如补问超限）保持原有提示，宁可多提示不漏提示。
    """
    if not task.degraded:
        return False
    errors = [str(e) for e in (task.errors or [])]
    if any(("回退" in e or "失败" in e or "超时" in e) for e in errors):
        return True
    if any("未配置 Key" in e for e in errors):
        return False
    return True


def _run_detail(task) -> str:
    """运行详情一行：任务编号与 Token 用量，只在折叠区出现，不占正文。"""
    parts = [f"任务 {task.task_id}"]
    if task.token_in or task.token_out:
        parts.append(f"Token 输入 {task.token_in} · 输出 {task.token_out}")
    return " · ".join(parts)


def _grade_badge(grade: str) -> str:
    """等级徽标；写法与历史页 `_status_badges` 的 markdown 徽标保持一致。"""
    for key, label, color in CLAUSE_GRADES:
        if key == grade:
            return f":{color}-badge[{label}]"
    return f":gray-badge[{grade or '未定级'}]"


def _group_undetermined(rows, reason: str) -> list[tuple[tuple[str, ...], list]]:
    """把同一类“判不了”的规则按缺失字段归组，排序固定，保证每次渲染顺序一致。"""
    grouped: dict[tuple[str, ...], list] = {}
    for row in rows or []:
        if str(getattr(row, "reason", "")) != reason:
            continue
        grouped.setdefault(tuple(row.missing_fields), []).append(row)
    return [(fields, sorted(grouped[fields], key=lambda r: r.rule_id)) for fields in sorted(grouped)]


def _field_label(fields: tuple[str, ...]) -> str:
    """缺失字段的中文名；字段为空（值或阈值解析不了）时给个不露 id 的说法。"""
    return "、".join(fact_display_name(f) for f in fields) or "相关事实"


def _rule_names(rows: list) -> str:
    return "、".join(str(r.name) for r in rows)


def _unmet_line(row, repo: SqliteRepository) -> str:
    """一条"合同未约定"：缺了哪项约定、因此判不了哪条要求、依据在哪。"""
    fields = "、".join(fact_display_name(f) for f in row.fields) or "相关事项"
    return f"未约定{fields}，无法判断是否满足“{row.name}”（依据 {source_label(repo, row.statute_id)}）"


def _render_unmet_terms(task, repo: SqliteRepository) -> None:
    """合同未约定区块：法律要求约定却没写的项目，逐条一行（不进“需补充事实”）。"""
    rows = sorted(list(getattr(task, "unmet", None) or []), key=lambda r: r.rule_id)
    if not rows:
        return
    st.markdown("**合同未约定**")
    for row in rows:
        st.caption(_unmet_line(row, repo))


def _render_clause_finding(finding, repo: SqliteRepository, *, badge: str = "") -> None:
    """单条条款结论：来源与等级 → 片段 → 理由 → 依据原文 → 待补事实。"""
    with st.container(border=True):
        head = _grade_badge(str(finding.grade))
        if badge:
            head = f"{badge} {head}"
        if str(getattr(finding, "source", "trigger")) == "model_extra":
            head += "（模型自提，待人工确认）"
        st.markdown(head)
        span = str(finding.span or "")
        clipped = span if len(span) <= CLAUSE_SPAN_LIMIT else span[:CLAUSE_SPAN_LIMIT] + "……"
        st.markdown(f"**条款片段**（正文第 {finding.paragraph} 段）：{clipped}")
        if len(span) > CLAUSE_SPAN_LIMIT:
            with st.expander("查看条款原文"):
                st.text(span)
        if finding.reason:
            st.markdown(f"**理由**：{finding.reason}")
        if finding.statute_ids:
            labels = [
                source_label(repo, sid) if repo.get_statute(sid) is not None else "法源库未找到该条文"
                for sid in finding.statute_ids
            ]
            st.markdown("**依据**：" + "、".join(labels))
            for sid in finding.statute_ids:
                statute = repo.get_statute(sid)
                if statute is None:
                    continue
                short = statute.law_name.replace("中华人民共和国", "")
                with st.expander(f"《{short}》{statute.article_no} 原文"):
                    st.markdown(statute.text)
                    st.caption(_statute_meta(statute))
        if finding.need_facts:
            st.caption("需补充事实：" + "、".join(finding.need_facts))


def _clause_review_note(task, clause_review: bool | None = None) -> str:
    """合并列表末尾的一句覆盖说明：条款审查做没做、有没有命中。

    `clause_review` 是本次运行的开关（当前会话有记录时传真实值），历史记录留空则读
    任务里落库的 `clause_review`——落库缺失（本篇之前的旧记录）按“未覆盖”处理：
    宁可口径保守，也不声称审查过而实际没做。
    """
    if list(getattr(task, "clause_findings", None) or []):
        return ""
    notes = [
        str(e) for e in (getattr(task, "errors", None) or []) if "条款审查" in str(e) or "条款判定" in str(e)
    ]
    # 只有“没跑起来 / 整批输出不可用”才算没做；“未命中候选”“逐条丢弃”都算跑过、没发现可疑条款。
    if any(marker in note for note in notes for marker in _NOT_REVIEWED_MARKERS):
        return CLAUSE_NOT_REVIEWED_NOTE
    if clause_review is None:
        enabled = bool(getattr(task, "clause_review", False))
    else:
        enabled = bool(clause_review)
    return CLAUSE_CLEAR_NOTE if enabled else CLAUSE_NOT_REVIEWED_NOTE


def _missing_clause_text(repo: SqliteRepository, term) -> str:
    """条文定位写成“第十七条第三项”；取不到条文号时退回条款序号原文。"""
    statute = repo.get_statute(term.statute_id)
    article = statute.article_no if statute is not None else ""
    body = str(term.clause_no or "").strip("（）() ")
    if article and body:
        return f"{article}第{body}项"
    return str(term.clause_no or "")


def _render_missing_term(term, repo: SqliteRepository, *, absent: bool) -> None:
    """单类必备条款：状态文案 + 条文定位 + 可展开的法条原文；内部编号不显示。"""
    where = _missing_clause_text(repo, term)
    if absent:
        st.error(f"缺失：未见{term.name}的约定（{where}）", icon=":material/error:")
    else:
        st.warning(
            f"未见{term.name}的约定（{where}），请人工确认",
            icon=":material/help:",
        )
    statute = repo.get_statute(term.statute_id)
    if statute is None:
        st.caption("依据条文未在法源库中找到，请人工核对。")
        return
    short = statute.law_name.replace("中华人民共和国", "")
    with st.expander(f"{term.name}：依据条文原文"):
        st.markdown(statute.text)
        st.caption(f"《{short}》{statute.article_no} · {_statute_meta(statute)}")


def _render_missing_terms(task, repo: SqliteRepository) -> None:
    """必备条款完整性：先说有没有写，再分缺失 / 未见 / 需人工确认；不判条款效力。"""
    terms = list(getattr(task, "missing_terms", None) or [])
    if not terms:
        return
    written = sum(
        1 for t in terms if t.status is MissingStatus.WRITTEN and t.term_id != MISSING_TERM_UNJUDGED_ID
    )
    absent = [t for t in terms if t.status is MissingStatus.ABSENT]
    unseen = [t for t in terms if t.status is MissingStatus.UNSEEN]
    manual = [t for t in terms if t.status is MissingStatus.MANUAL]
    st.markdown("#### :material/checklist: 必备条款完整性")
    if not absent and not unseen and not written:
        st.caption("正文过短，未做必备条款判定。")
        return
    st.caption(
        f"本次检查《劳动合同法》第十七条九类必备条款：已写 {written} 类，缺失 {len(absent)} 类，"
        f"未见 {len(unseen)} 类，需人工确认 {len(manual)} 类。{MISSING_TERMS_BASE_NOTE}"
    )
    for term in absent:
        _render_missing_term(term, repo, absent=True)
    for term in unseen:
        _render_missing_term(term, repo, absent=False)
    if manual:
        st.caption(f"需人工确认 {len(manual)} 类：" + "、".join(t.name for t in manual))


def _optional_picks(task, repo: SqliteRepository) -> list[str]:
    """本次任务的可补充字段：先按规则影响排序，再由模型在候选内挑最多 3 项。

    结果按任务缓存，避免每次重绘结果页都调一次模型。
    """
    cache = st.session_state.setdefault("optional_picks", {})
    key = str(task.task_id)
    if key in cache:
        return list(cache[key])
    facts = {str(k): str(v) for k, v in (task.facts or {}).items()}
    candidates = candidate_fields(tuple(repo.list_rules()), facts, limit=MAX_ASK_CANDIDATES)
    picked: list[str] = []
    if candidates:
        llm_cfg = _resolve_llm(get_config(), repo)
        generator = DeepSeekLLM(llm_cfg) if llm_cfg is not None else None
        picked, _errors = suggest_optional(generator, facts, candidates)
    cache[key] = list(picked)
    return list(picked)


def _render_optional_facts(task, repo: SqliteRepository, user: User) -> None:
    """可补充事实面板：补充后按同一份合同重算，属于补充而不是阻断追问。

    只在对不上号的事实真正影响判定时才出现（`field_missing`），不填也能拿到结论。
    """
    if not any(str(getattr(row, "reason", "")) == "field_missing" for row in task.undetermined or []):
        return
    picks = _optional_picks(task, repo)
    if not picks:
        return
    st.markdown("**可选补充**")
    form_key = f"more_{task.task_id}"
    with st.form(form_key):
        values: dict[str, str] = {}
        for field_id in picks:
            values[field_id] = st.text_input(
                field_name(field_id),
                key=f"{form_key}_{field_id}",
                placeholder=field_hint(field_id) or "请填写",
                help=field_question(field_id),
            )
        submitted = st.form_submit_button("补充并重新核对", type="primary")
    if not submitted:
        return
    extra = {field_id: str(value).strip() for field_id, value in values.items() if str(value).strip()}
    if not extra:
        st.caption("未填写任何内容，本次未重新核对。")
        return
    contract_text = str(st.session_state.get("ask_contract") or "")
    if not contract_text.strip():
        st.warning("缺少合同正文，无法重新核对，请重新提交合同。", icon=":material/warning:")
        return
    facts = {str(k): str(v) for k, v in (task.facts or {}).items()}
    facts.update(extra)
    with st.spinner("按补充事实重新核对……"):
        new_task = run_check(
            user=user,
            contract_text=contract_text,
            facts=facts,
            repo=repo,
            enterprise_id=user.enterprise_id,
            ask_rounds=int(task.ask_rounds or 0),
            max_ask_rounds=1,
            contract_name=str(st.session_state.get("ask_contract_name") or task.contract_name),
            contract_id=task.contract_id,
            llm=_resolve_llm(get_config(), repo),
            clause_review=st.session_state.get("ask_clause_review"),
        )
    st.session_state["last_task"] = new_task
    st.rerun()


def _render_result_disclaimer() -> None:
    """结果页最底部常驻免责：每个核对结果页只出现一次。"""
    st.caption(RESULT_DISCLAIMER)


def _render_task(
    task, repo: SqliteRepository, user: User | None = None, clause_review: bool | None = None
) -> None:
    """结果页渲染；`clause_review` 传本次会话的开关，历史记录不传（改读落库的 `task.clause_review`）。"""
    if task.refused:
        if task.boundary_layer == "llm":
            st.error(
                f"无法生成回答：{task.boundary_reason or '未配置大模型或调用失败'}",
                icon=":material/smart_toy:",
            )
            st.caption("按“无依据不答”原则不给出生成式回答；请在设置页配置 DeepSeek Key 后重试。")
        else:
            st.error(
                f"越界拒答（判定层：{task.boundary_layer or '未记录'}）："
                f"{task.boundary_reason or '问题超出劳动用工范围'}",
                icon=":material/block:",
            )
            st.caption(
                "本系统只处理劳动用工与劳动合同问题；其他问题建议拨打 12348 公共法律服务热线或咨询专业人士。"
            )
        return

    cited = {i.source_id for i in task.items}
    coverage = task.rule_coverage
    judged = coverage.violated + coverage.compliant + coverage.not_applicable if coverage else 0
    with st.container(horizontal=True):
        st.metric("风险条目", len(task.items), border=True)
        st.metric("处理建议", len(task.suggestions), border=True)
        st.metric("引用条文", len(cited), border=True)
        st.metric("耗时", f"{task.latency_s or 0:.2f} 秒", border=True)
        if coverage is not None and coverage.total:
            st.metric("规则可判定", f"{judged}/{coverage.total}", border=True)
        if task.missing_terms and task.missing_coverage is not None:
            written_count = sum(1 for t in task.missing_terms if t.status is MissingStatus.WRITTEN)
            st.metric("必备条款", f"{written_count}/{MISSING_TERM_BASE}", border=True)

    with st.container(horizontal=True, vertical_alignment="center"):
        st.download_button(
            "导出 Word 报告",
            data=build_report_docx(task, repo),
            file_name=f"核对报告-{task.task_id}.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            icon=":material/download:",
        )

    with st.container(horizontal=True):
        st.badge(
            "依据校验通过" if task.verify_ok else "依据校验未通过",
            icon=":material/verified:" if task.verify_ok else ":material/error:",
            color="green" if task.verify_ok else "red",
        )
        st.badge(
            f"补问 {task.ask_rounds} 轮" if task.ask_rounds else "无补问",
            icon=":material/help:",
            color="blue" if task.ask_rounds else "gray",
        )
        if _show_degraded_badge(task):
            st.badge("已降级", icon=":material/warning:", color="orange")

    errors = [str(e) for e in (task.errors or [])]
    extract_note = ""
    if any(("回退" in e or "失败" in e or "超时" in e) for e in errors):
        extract_note = "抽取已回退规则式：非常规写法的条款可能未被识别，请人工复核。"
    elif any("未配置 Key" in e for e in errors):
        extract_note = "本次未使用模型抽取：未配置 Key"
    if extract_note:
        st.caption(extract_note)

    if task.facts:
        with st.popover(f"依据事实（{len(task.facts)} 项）", icon=":material/fact_check:"):
            st.dataframe(
                [{"事实": fact_display_name(k), "值": v} for k, v in task.facts.items()],
                width="stretch",
                hide_index=True,
            )
    with st.expander("详情", icon=":material/info:"):
        st.caption(_run_detail(task))
    if task.questions:
        st.info(
            f"本次提出 {len(task.questions)} 个追问：" + "；".join(task.questions),
            icon=":material/help:",
        )

    field_groups = _group_undetermined(task.undetermined, "field_missing")
    premise_groups = _group_undetermined(task.undetermined, "premise_missing")
    field_count = sum(len(rows) for _, rows in field_groups)

    with st.container(border=True):
        st.markdown(
            "#### :material/rule: 核对结论",
            help="等级含义：A 与法条明文冲突，B 显著失衡，C 待确认；只表示与法条存在冲突的风险。",
        )
        findings = list(task.clause_findings or [])
        for rank, entry in merge_findings(task.items, findings):
            if rank == 0:
                st.markdown(f":blue-badge[{FINDING_SOURCE_BADGES[0]}] **{entry.finding}**")
                cite = f"依据 {source_label(repo, entry.source_id)}"
                statute = repo.get_statute(entry.source_id)
                if statute is not None and statute.source_url:
                    cite += f" · 来源：{statute.source_url}"
                st.caption(cite)
                continue
            color = FINDING_SOURCE_COLORS[rank]
            _render_clause_finding(entry, repo, badge=f":{color}-badge[{FINDING_SOURCE_BADGES[rank]}]")
        note = _clause_review_note(task, clause_review)
        if note:
            st.caption(note)
        if not task.items:
            if field_count:
                st.warning(
                    f"本次未发现硬性风险项，但仍有 {field_count} 条规则因缺少事实无法判定，不代表合规。",
                    icon=":material/help:",
                )
            else:
                st.info("未发现可判定的硬性风险项（不代表合规，请结合事实复核）。", icon=":material/info:")
        if not task.items and 0 < task.contract_chars < 200:
            st.caption(f"合同正文仅 {task.contract_chars} 字，条款覆盖有限；0 风险不等于合同合规。")

        _render_missing_terms(task, repo)
        _render_unmet_terms(task, repo)

        if field_groups or premise_groups:
            st.markdown("**需补充事实**")
            for fields, rows in field_groups:
                st.caption(
                    f"因缺少“{_field_label(fields)}”，以下 {len(rows)} 条规则无法判定：{_rule_names(rows)}"
                )
            if premise_groups:
                premise_count = sum(len(rows) for _, rows in premise_groups)
                with st.expander(f"另有 {premise_count} 条规则因前提未确定而未判定", expanded=False):
                    for fields, rows in premise_groups:
                        label = _field_label(fields)
                        st.caption(f"因缺少“{label}”，以下 {len(rows)} 条规则未判定：{_rule_names(rows)}")

        if task.statutory_na:
            st.caption(f"另有 {task.statutory_na} 条规则因合同未约定而适用法定标准。")

    with st.container(border=True):
        st.markdown(
            "#### :material/tips_and_updates: 处理建议（非法律意见）",
            help="建议不构成法律意见，重大争议请咨询专业人士。",
        )
        if task.suggestions:
            for s in task.suggestions:
                cites = "、".join(source_label(repo, sid) for sid in s.source_ids)
                st.markdown(f"- {s.text}")
                if s.disclaimer and s.disclaimer != DISCLAIMER_FALLBACK:
                    st.caption(f"依据 {cites} · {s.disclaimer}")
                else:
                    st.caption(f"依据 {cites}")
        else:
            if any("未配置大模型" in e for e in task.errors):
                st.caption("未配置大模型：本次不生成处理建议（规则核对结论不受影响）。")
            else:
                st.caption("本次没有可给出的建议（无依据不生成）。")

    with st.container(border=True):
        cases = repo.list_cases()
        shown: list = []
        note = ""
        if cases:
            by_id = {c.case_id: c for c in cases}
            shown = [by_id[r.case_id] for r in task.case_refs if r.case_id in by_id]
            if shown:
                note = f"共 {len(cases)} 条官方案例，本次匹配 {len(shown)} 条"
            else:
                cited_ids = {i.source_id for i in task.items}
                matched = [c for c in cases if set(c.statute_ids) & cited_ids]
                shown = (matched or cases)[:5]
                note = f"共 {len(cases)} 条官方案例，按法条匹配 {len(matched)} 条"
        st.markdown("#### :material/library_books: 类案参照", help=f"{CASE_NOTE} {note}".strip())
        if cases:
            for case in shown:
                st.markdown(f"**{case.title}**")
                st.caption(case.holding)
                meta = f"{case.source} · {case.dispute_type}"
                if case.source_url:
                    meta += f" · 来源：{case.source_url}"
                st.caption(meta)
        else:
            st.caption("暂无官方案例，当前结论全部来自法条。")

    if user is not None:
        _render_qa_panel(task, repo, user)

    # 免责必须在所有区块之后：问答面板也归这一页，问答结论同样被这一行覆盖。
    _render_result_disclaimer()


# ---------------- 历史 ----------------
def _fmt_dt(value) -> str:
    if isinstance(value, datetime):
        if value.hour == 0 and value.minute == 0:
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value or "时间未知")


def _status_badges(task) -> str:
    if task.refused:
        return ":red-badge[越界拒答]"
    parts = [":green-badge[依据通过]" if task.verify_ok else ":red-badge[依据未通过]"]
    if _show_degraded_badge(task):
        parts.append(":orange-badge[已降级]")
    if task.ask_rounds:
        parts.append(f":blue-badge[补问 {task.ask_rounds} 轮]")
    return " ".join(parts)


def _page_history(repo: SqliteRepository, user: User) -> None:
    page_header("history", "核对历史")
    ent_id = user.enterprise_id or ""
    if not ent_id:
        with st.container(border=True):
            st.warning(
                "当前账号未绑定企业。核对记录按企业隔离，先完善企业档案才能查看。",
                icon=":material/domain:",
            )
            st.page_link(NAV["企业档案"], label="去完善企业档案", icon=":material/arrow_forward:")
        return
    checks = [t for t in repo.list_tasks(ent_id, limit=200) if t.mode is TaskMode.CHECK]
    _, deleted = repo.count_tasks(ent_id)

    if not checks:
        with st.container(border=True):
            st.info("还没有核对记录。", icon=":material/history:")
            st.page_link(NAV["合同核对"], label="去合同核对", icon=":material/arrow_forward:")
        return

    pending = st.session_state.get("pending_delete")
    if pending:
        st.warning(
            f"确认删除记录 {pending}？删除后历史页不再显示，操作日志仍保留。",
            icon=":material/delete:",
        )
        c1, c2, _ = st.columns([1, 1, 4])
        if c1.button("确认删除", type="primary", key="confirm_del"):
            repo.delete_task(pending, ent_id)
            st.session_state.pop("pending_delete", None)
            if st.session_state.get("view_task_id") == pending:
                st.session_state.pop("view_task_id", None)
            st.rerun()
        if c2.button("取消", key="cancel_del"):
            st.session_state.pop("pending_delete", None)
            st.rerun()

    choice = st.segmented_control(
        "筛选",
        ["全部", "有风险", "已降级"],
        default="全部",
        label_visibility="collapsed",
    )
    shown = checks
    if choice == "有风险":
        shown = [t for t in checks if t.items]
    elif choice == "已降级":
        shown = [t for t in checks if t.degraded]

    st.caption(f"共 {len(shown)} 次有效核对 · 已排除 {deleted} 条已删除记录 · 本页仅显示核对记录")

    for task in shown[:20]:
        with st.container(border=True):
            left, mid, right = st.columns([2.6, 1.6, 1.2], vertical_alignment="center")
            with left:
                st.markdown(f"**{task.contract_name or '未命名合同'}**")
                st.caption(
                    f"{_fmt_dt(task.created_at)} · {len(task.items)} 条风险 · "
                    f"{len(task.suggestions)} 条建议 · {task.latency_s or 0:.2f} 秒"
                )
            with mid:
                st.markdown(_status_badges(task))
            with right:
                with st.container(horizontal=True):
                    if st.button("查看", key=f"view_{task.task_id}", icon=":material/visibility:"):
                        st.session_state["view_task_id"] = task.task_id
                        st.rerun()
                    if st.button("删除", key=f"del_{task.task_id}", icon=":material/delete:"):
                        st.session_state["pending_delete"] = task.task_id
                        st.rerun()

    view_id = st.session_state.get("view_task_id")
    task = next((t for t in checks if t.task_id == view_id), None)
    if task is not None:
        st.subheader("记录详情", icon=":material/receipt_long:", divider="gray", anchor=False)
        _render_task(task, repo)
        if st.button("收起详情", icon=":material/keyboard_arrow_up:"):
            st.session_state.pop("view_task_id", None)
            st.rerun()

    with st.expander("统计与趋势", icon=":material/trending_up:"):
        frame = trend_frame(checks)
        if not frame.empty:
            risks = [int(v) for v in frame["风险条目"].tolist()]
            with st.container(horizontal=True):
                st.metric("最近一次", f"{risks[-1]} 条", border=True)
                st.metric("平均", f"{sum(risks) / len(risks):.1f} 条", border=True)
                st.metric("最多", f"{max(risks)} 条", border=True)
            st.altair_chart(_trend_chart(frame), width="stretch")
            st.caption(
                f"统计窗口：最近 {len(frame)} 次有效核对；平均值含真实 0 风险记录，分母为 {len(frame)}。"
            )


# ---------------- 就本合同提问 ----------------
def _qa_context(task) -> dict:
    """问答上下文：只取结构化结论（事实、规则命中、条款结论的段落号与等级）。

    合同正文与条款片段不在这里：问答的检索路与生成路都只用这份摘要，
    不接收合同原文。
    """
    return {
        "facts": dict(task.facts or {}),
        "items": [{"rule_id": item.rule_id, "statute_id": item.source_id} for item in (task.items or [])],
        "clauses": [
            {
                "paragraph": finding.paragraph,
                "grade": str(finding.grade),
                "trigger_id": finding.trigger_id,
                "statute_ids": list(finding.statute_ids),
            }
            for finding in (task.clause_findings or [])
        ],
    }


def _qa_detail(task) -> str:
    """问答详情一行：工具调用与 Token 用量，只在折叠区出现。"""
    parts: list[str] = []
    if task.agent_trace:
        parts.append("工具调用 " + " → ".join(task.agent_trace))
    if task.token_in or task.token_out:
        parts.append(f"Token 输入 {task.token_in} · 输出 {task.token_out}")
    return " · ".join(parts)


def _render_qa_panel(task, repo: SqliteRepository, user: User) -> None:
    """核对结果底部的“就本合同提问”面板：结合本次结构化结论作答，不发合同正文。"""
    stored = st.session_state.get("last_qa_context")
    has_result = isinstance(stored, dict) and stored.get("task_id") == task.task_id
    with st.expander("就本合同继续提问", icon=":material/chat:", expanded=has_result):
        with st.form(f"qa_{task.task_id}"):
            question = st.text_area(
                "问题",
                height=110,
                placeholder="例如：这份合同的试用期约定有什么风险？",
                help="回答基于本次核对的结构化事实与命中法条，不发送合同正文。",
            )
            asked = st.form_submit_button("提问", type="primary", icon=":material/send:")
        if asked:
            text = str(question or "").strip()
            if not text:
                st.warning("请先输入问题。", icon=":material/warning:")
            else:
                with st.spinner("检索法条中……"):
                    answer = run_qa(
                        user=user,
                        question=text,
                        repo=repo,
                        enterprise_id=user.enterprise_id,
                        llm=_resolve_llm(get_config(), repo),
                        context=_qa_context(task),
                    )
                st.session_state["last_qa_context"] = {"task_id": task.task_id, "qa": answer}
                st.rerun()
        result = st.session_state.get("last_qa_context")
        if isinstance(result, dict) and result.get("task_id") == task.task_id:
            qa_task = result.get("qa")
            if qa_task is not None:
                _render_qa(qa_task, repo)


def _render_qa(task, repo: SqliteRepository) -> None:
    """输出物理分两块：“结论与依据（法条原文）”与“处理建议（非法律意见）”。"""
    if task.refused:
        if task.boundary_layer == "llm":
            st.error(
                f"无法生成回答：{task.boundary_reason or '未配置大模型或调用失败'}",
                icon=":material/smart_toy:",
            )
            st.caption("按“无依据不答”原则不给出生成式回答；请在设置页配置 DeepSeek Key 后重试。")
        else:
            st.error(
                f"越界拒答（判定层：{task.boundary_layer or '未记录'}）："
                f"{task.boundary_reason or '问题超出劳动用工范围'}",
                icon=":material/block:",
            )
            st.caption(
                "本系统只处理劳动用工与劳动合同问题；其他问题建议拨打 12348 公共法律服务热线或咨询专业人士。"
            )
        return

    if not task.suggestions:
        st.warning(
            "没有检索到可引用的现行条文，已按“无依据不答”处理；可补充事实（如合同期限、"
            "是否书面签订）后重问，或转专业咨询。",
            icon=":material/search_off:",
        )
        if task.errors:
            st.caption(" · ".join(task.errors))
        return

    sources: list[str] = []
    for s in task.suggestions:
        for sid in s.source_ids:
            if sid not in sources:
                sources.append(sid)

    with st.container(border=True):
        st.markdown("#### :material/menu_book: 结论与依据（法条原文）")
        for sid in sources:
            statute = repo.get_statute(sid)
            if statute is None:
                st.caption("依据条文未在法源库中找到，请人工核对。")
                continue
            short = statute.law_name.replace("中华人民共和国", "")
            with st.expander(f"《{short}》{statute.article_no}", expanded=len(sources) == 1):
                st.markdown(statute.text)
                st.caption(_statute_meta(statute))

    with st.container(border=True):
        st.markdown("#### :material/tips_and_updates: 处理建议（非法律意见）")
        for s in task.suggestions:
            cites = "、".join(source_label(repo, sid) for sid in s.source_ids)
            st.markdown(f"- {s.text}")
            if s.disclaimer and s.disclaimer != DISCLAIMER_FALLBACK:
                st.caption(f"依据 {cites} · {s.disclaimer}")
            else:
                st.caption(f"依据 {cites}")
        if task.agent_trace or task.token_in or task.token_out:
            with st.expander("详情", icon=":material/info:"):
                st.caption(_qa_detail(task))


# ---------------- 设置 ----------------
def _settings_flash() -> None:
    """保存 / 清除 / 测试连接后的提示：重跑一次再显示，避免提示跟着旧值一起刷新掉。"""
    if msg := st.session_state.pop("settings_flash", ""):
        st.success(msg, icon=":material/check_circle:")
    if msg := st.session_state.pop("settings_error", ""):
        st.error(msg, icon=":material/error:")


def _render_model_settings(repo: SqliteRepository) -> None:
    """模型 Key（仅管理员）：落库后下次启动自动读取；清除即回落到下一优先级。

    这是模型设置的唯一入口：侧栏不再提供会话级 Key，测试连接也放在这里。
    """
    sources = llm_key_sources(repo)
    st.markdown("#### :material/smart_toy: 模型设置")
    st.caption(f"当前 Key 来源：{key_source_label(sources)}")
    st.caption("优先级 secrets.toml > 数据库 > 环境变量")
    with st.form("llm_key_form"):
        new_key = st.text_input(
            "DeepSeek API Key",
            type="password",
            placeholder="sk-……（保存在本机数据库，不写日志）",
        )
        saved = st.form_submit_button("保存", type="primary", icon=":material/save:")
    if saved:
        key = new_key.strip()
        if not key:
            st.warning("请先输入 Key；要清除已保存的 Key，用下面的清除按钮。", icon=":material/warning:")
        else:
            repo.set_setting(LLM_KEY_SETTING, key)
            st.session_state["settings_flash"] = "已保存到本机数据库，下次启动自动读取"
            st.rerun()
    if st.button("清除数据库中的 Key", icon=":material/delete:"):
        repo.set_setting(LLM_KEY_SETTING, "")
        st.session_state["settings_flash"] = "已清除数据库中的 Key，回落到下一优先级"
        st.rerun()

    llm = _resolve_llm(get_config(), repo)
    if st.button("测试连接", icon=":material/network_check:", disabled=llm is None):
        with st.spinner("连接 DeepSeek……"):
            probe = DeepSeekLLM(llm) if llm is not None else None
            ok = probe.health() if probe is not None else False
        if ok:
            st.session_state["settings_flash"] = "连接正常"
        else:
            detail = (probe.last_error if probe is not None else "未配置 Key")[:80]
            st.session_state["settings_error"] = f"连接失败：{detail}"
        st.rerun()
    if llm is None:
        st.caption("请先保存 Key，再测试连接。")
    emb = get_config().embedding
    if emb.api_key:
        st.caption(f"向量检索：{emb.model}")
    else:
        st.caption("向量检索：未配置 Key，回退 BM25")
    st.caption("只接 DeepSeek；Key 不写日志。")


def _render_account_settings(repo: SqliteRepository, user: User) -> None:
    st.markdown("#### :material/person: 账号设置")
    st.text_input("用户名", value=user.username, disabled=True)
    with st.form("pwd_form"):
        old = st.text_input("当前密码", type="password")
        new = st.text_input(f"新密码（至少 {MIN_PASSWORD_LEN} 位）", type="password")
        confirm = st.text_input("确认新密码", type="password")
        changed = st.form_submit_button("修改密码", type="primary", icon=":material/key:")
    if changed:
        if new != confirm:
            st.error("两次输入的新密码不一致", icon=":material/error:")
            return
        ok, msg = change_password(repo, user, old, new)
        if ok:
            st.success("密码已修改，下次登录请使用新密码。", icon=":material/check_circle:")
        else:
            st.error(msg, icon=":material/error:")


def _page_settings(repo: SqliteRepository, user: User) -> None:
    """设置页：模型 Key 只给管理员；改密码所有登录用户都能用。"""
    page_header("settings", "设置")
    _settings_flash()
    if is_admin(user):
        _render_model_settings(repo)
    _render_account_settings(repo, user)


# ---------------- 管理端 ----------------
def slot_table_rows(rules) -> list[dict[str, str]]:
    """必问事实清单的表格行：按规则表推导槽位，让每个槽位带上真实引用它的规则 id。"""
    return [
        {
            "槽位": s.slot_id,
            "名称": s.name,
            "类型": "阻断项" if s.blocking else "参考项",
            "追问句": s.question,
            "被哪些规则使用": "、".join(s.needed_by),
        }
        for s in derive_slots(tuple(rules), {})
    ]


def _page_admin(repo: SqliteRepository, user: User) -> None:
    page_header("admin_panel_settings", "管理端")
    try:
        require_admin(user)
    except PermissionError as exc:
        st.error(str(exc), icon=":material/lock:")
        return

    tab_users, tab_statutes, tab_rules, tab_logs, tab_eval = st.tabs(
        [
            ":material/group: 用户",
            ":material/menu_book: 法源库",
            ":material/rule: 规则表",
            ":material/receipt_long: 操作日志",
            ":material/science: 评测",
        ]
    )
    with tab_users:
        users = repo.list_users()
        st.dataframe(
            [{"用户名": u.username, "角色": str(u.role), "企业": u.enterprise_id} for u in users],
            width="stretch",
            hide_index=True,
        )
        c1, c2, c3 = st.columns([2, 2, 1], vertical_alignment="bottom")
        with c1:
            target = st.selectbox("选择用户", [u.username for u in users])
        with c2:
            role = st.selectbox("新角色", [str(Role.USER), str(Role.ADMIN)])
        with c3:
            if st.button("保存", type="primary"):
                u = repo.get_user_by_name(target)
                if u:
                    repo.set_user_role(u.user_id, role)
                    st.rerun()

        if st.session_state.get("admin_delete_flash"):
            st.success(st.session_state.pop("admin_delete_flash"))
        with st.expander("删除账号（不可恢复）", icon=":material/delete_forever:"):
            st.caption("删除后该账号及其企业、合同、任务、日志一并移除，不可恢复；操作会写一条管理员日志。")
            delete_target = st.selectbox(
                "要删除的账号",
                [u.username for u in users],
                key="admin_delete_target",
            )
            # 删除成功后要把确认框清空：控件已实例化时不能直接改它的会话值，
            # 所以先置重置标记，在下一次运行、控件创建之前清空。
            if st.session_state.get("admin_delete_reset"):
                st.session_state["admin_delete_confirm"] = ""
                st.session_state["admin_delete_reset"] = False
            confirm = st.text_input(
                "输入目标用户名以确认",
                key="admin_delete_confirm",
                placeholder=delete_target,
            )
            if st.button(
                "确认删除账号",
                type="primary",
                key="admin_delete_btn",
                disabled=not confirm or confirm != delete_target,
            ):
                try:
                    result = delete_account(repo, user, delete_target)
                except PermissionError as exc:  # 理论上进不来，防御性处理
                    st.error(str(exc), icon=":material/lock:")
                else:
                    if result.deleted:
                        st.session_state["admin_delete_flash"] = (
                            f"已删除账号 {delete_target}：合同 {result.contracts} 条、"
                            f"任务 {result.tasks} 条、日志 {result.logs} 条"
                        )
                        st.session_state["admin_delete_reset"] = True
                        st.rerun()
                    else:
                        st.error(result.reason)
    with tab_statutes:
        statutes = repo.list_statutes()
        kw = st.text_input("按关键词过滤（法名 / 条号 / 正文）", placeholder="例如：试用期")
        rows = [
            {
                "ID": s.statute_id,
                "法名": s.law_name,
                "条": s.article_no,
                "状态": str(s.status),
                "施行": str(s.effective_date or ""),
            }
            for s in statutes
            if not kw or kw in s.law_name or kw in s.article_no or kw in s.text
        ]
        st.caption(f"共 {len(statutes)} 条，筛出 {len(rows)} 条")
        st.dataframe(rows[:200], width="stretch", hide_index=True)
    with tab_rules:
        st.dataframe(
            [
                {
                    "规则": r.rule_id,
                    "法条": r.statute_id,
                    "字段": r.field,
                    "条件": f"{r.op} {r.threshold}{r.unit}",
                    "依据片段": r.evidence_span,
                }
                for r in repo.list_rules()
            ],
            width="stretch",
            hide_index=True,
        )
        st.markdown("#### 必问事实清单（slot schema）")
        st.caption("阻断项缺失时必须追问；参考项顺带问，缺了不阻断结论。")
        st.dataframe(
            slot_table_rows(repo.list_rules()),
            width="stretch",
            hide_index=True,
        )
    with tab_logs:
        logs = repo.list_logs(500)
        f1, f2 = st.columns([1, 2], vertical_alignment="bottom")
        with f1:
            action = st.selectbox("动作", ["全部", "核对", "问答"])
        with f2:
            keyword = st.text_input("关键词（用户 / 任务 / 详情）", placeholder="例如：admin 或 T-")
        rows = [
            log
            for log in logs
            if (action == "全部" or log.action == action)
            and (not keyword or keyword in log.user_id or keyword in log.task_id or keyword in log.detail)
        ]
        st.caption(f"共 {len(logs)} 条 · 筛出 {len(rows)} 条")
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["时间", "用户", "动作", "任务", "详情"])
        for log in rows:
            writer.writerow([log.created_at, log.user_id, log.action, log.task_id, log.detail])
        st.download_button(
            "导出 CSV",
            data=buffer.getvalue().encode("utf-8-sig"),
            file_name="操作日志.csv",
            mime="text/csv",
            icon=":material/download:",
        )
        st.dataframe(
            [
                {
                    "时间": log.created_at,
                    "用户": log.user_id,
                    "动作": log.action,
                    "任务": log.task_id,
                    "详情": log.detail,
                }
                for log in rows
            ],
            width="stretch",
            hide_index=True,
        )
    with tab_eval:
        _render_eval()


EVAL_FILE = Path(data_path("evalsets/last_eval.json"))
EVAL_LABELS = {
    "accuracy": "风险/缺项识别准确率",
    "recall": "风险/缺项召回率",
    "traceable": "依据可追溯率",
    "ask_trigger": "事实补问触发率",
    "no_false_ask": "不误问率",
    "refuse": "越界拒答率",
    "false_refuse": "误拒率",
    "latency_s": "首轮处理时间（秒）",
    "unsourced_suggestion": "建议无依据率",
    "statute_hit": "法条召回率（诊断）",
}
EVAL_LOWER_BETTER = ("false_refuse", "latency_s", "unsourced_suggestion")


def _render_eval() -> None:
    """只读展示最近一次评测结果（由 scripts/run_eval.py 生成，不在界面里跑评测）。"""
    st.markdown("#### 评测结果")
    if not EVAL_FILE.exists():
        st.info("还没有评测结果。先在项目根目录运行：`python scripts/run_eval.py`", icon=":material/science:")
        return
    data = json.loads(EVAL_FILE.read_text(encoding="utf-8"))
    variants = list((data.get("variants") or {}).keys())
    if not variants:
        st.info("评测文件为空。", icon=":material/science:")
        return
    variant = st.selectbox("消融档位", variants, index=variants.index("full") if "full" in variants else 0)
    result = data["variants"][variant]
    rows = []
    for key, value in result["metrics"].items():
        threshold = ACCEPTANCE.get(key)
        passed = result["pass"].get(key)
        rows.append(
            {
                "指标": EVAL_LABELS.get(key, key),
                "实测": f"{value:.4f}" if isinstance(value, float) else value,
                "阈值": f"{threshold:g}" if threshold is not None else "未设",
                "达标": "达标" if passed else ("未达" if passed is False else "未设"),
            }
        )
    st.dataframe(rows, width="stretch", hide_index=True)
    st.caption(
        f"样本 {data.get('case_count', 0)} 条 · 生成时间 {data.get('generated_at', '')} · "
        "回归集为官方示范文本加人工植入；留出集与问答消融的标注说明见 src/mla/data/evalsets/README.md。"
    )
    mismatches = result.get("mismatches") or []
    if mismatches:
        with st.expander(f"不一致样本（{len(mismatches)} 条）"):
            st.dataframe(mismatches, width="stretch", hide_index=True)

    history_file = Path(data_path("evalsets/eval_history.jsonl"))
    if history_file.exists():
        lines = [x for x in history_file.read_text(encoding="utf-8").splitlines() if x.strip()]
        with st.expander(f"历史评测记录（{len(lines)} 次）", icon=":material/history:"):
            history_rows = []
            for line in lines[-10:]:
                row = json.loads(line)
                for name, result_row in (row.get("variants") or {}).items():
                    metrics = result_row.get("metrics") or {}
                    passed = result_row.get("pass") or {}
                    history_rows.append(
                        {
                            "时间": row.get("generated_at", ""),
                            "样本": row.get("case_count", 0),
                            "档位": name,
                            "准确率": metrics.get("accuracy"),
                            "召回率": metrics.get("recall"),
                            "达标项": f"{sum(1 for v in passed.values() if v)}/{len(passed)}",
                        }
                    )
            st.dataframe(history_rows, width="stretch", hide_index=True)


def main() -> None:
    cfg = get_config()
    repo = get_repo(cfg.paths.db)
    user = st.session_state.get("user")
    if user is None:
        # 库里还没有管理员就是首次使用：先建号，不显示登录表单。
        if not has_admin(repo):
            _init_screen(repo)
            return
        _login_screen(repo)
        return
    _sidebar_chrome()
    if _should_onboard(user):
        # 模态弹窗：确认前页面其它控件点不动，但渲染不受影响。
        _onboarding_dialog(repo, user)
    nav = _build_nav(repo, user)
    _sidebar_footer(user)
    nav.run()


if __name__ == "__main__":
    main()
