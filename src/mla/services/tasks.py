"""任务编排服务：跑状态图、计时、落库、写操作日志（模块 4/5/6/10 的服务层）。

UI 与 CLI 都调这里，保证「同样的输入 → 同样的记录」。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

from ..adapters.extract import RegexExtractor
from ..adapters.extract_llm import LLMExtractor
from ..adapters.llm import DeepSeekLLM
from ..config import LLMConfig, env_flag, load_config
from ..domain.boundary import classify_by_terms
from ..domain.clauses import detect_clauses, load_triggers
from ..domain.models import CheckItem, CheckTask, ClauseFinding, OperationLog, TaskMode, User
from ..domain.rules import RuleCheck, load_rules
from ..graph.build import build_graph
from ..graph.nodes import Deps, consensus
from ..graph.state import CheckState
from ..ports.extraction import Extractor
from ..ports.repository import Repository
from .clause_review import RetryStats, review_clauses
from .credentials import resolve_llm_config
from .retrieval import build_retriever

# 条款审查启用时，这条规则让位给触发器 TR-PROBATION-WAGE-BASE + 模型判定
# （规格 1.4：规则只看比例不看基数，会把"按基本工资 80%"误判为合规）。
# 让位是**事后去重**：规则始终参与判定，只有模型真的给出同域结论时才剔除规则条目；
# 模型失败 / 未启用 / 未命中触发器时规则照常报，避免这条最清晰的风险静默消失。
PROBATION_WAGE_RULE_ID = "R-PROBATION-WAGE-80"
PROBATION_WAGE_TRIGGER_ID = "TR-PROBATION-WAGE-BASE"

# 无模型门禁的提示语（唯一一处定义：CLI 打印、异常信息、测试断言都引用它）
NO_MODEL_MESSAGE = "未配置模型，无法发起核对；请在设置页配置 API Key"
NO_MODEL_QA_MESSAGE = "未配置模型，无法发起问答；请在设置页配置 API Key"


class NoModelError(RuntimeError):
    """服务层硬门禁：没有可用模型时拒绝执行核心能力（规格 003 第 8 章）。

    UI 的门禁提示只是友好层，这里才是唯一真门禁：UI / CLI / 其他调用方都走服务入口，
    所以断言放在 `run_check` / `run_qa` 的第一行，绕不过去。
    离线测试与离线诊断用 `MLA_ALLOW_NO_LLM=1` 显式放行，放行后行为与旧路径完全一致。
    """


def allow_no_model() -> bool:
    """离线开关：`MLA_ALLOW_NO_LLM` 取 1 / true / on / yes 时允许无模型执行旧路径。"""
    return env_flag("MLA_ALLOW_NO_LLM")


def has_model(llm: LLMConfig | None = None) -> bool:
    """是否有可用模型：与 `_deps` 同一判定口径（resolver 解析到 Key 才算有模型）。

    不传 `llm` 时走统一 resolver（env > auth.json > 阶段三旧库兜底），不再直接读
    `load_config().llm.api_key`——那会绕过 resolver 的优先级。
    """
    if llm is not None:
        return bool(llm.api_key)
    llm_cfg, _resolved = resolve_llm_config()
    return llm_cfg is not None


def model_first_enabled() -> bool:
    """是否走**模型主导路径**（规格 003 第 13 章 P1-3：`items` 只由共识层产出）。

    默认开：只有有模型时才谈得上模型主导（共识层的两条输入才齐）；
    `MLA_ALLOW_NO_LLM=1`（离线测试 / 离线诊断）**强制关**——那种场景没有模型结论，
    条目只能来自规则通道，否则规则结论会被共识层静默丢掉；
    显式 `MLA_MODEL_FIRST=off` 也回旧路径（双写时代的 items + verdict 旧算法），供回滚用。
    """
    return env_flag("MLA_MODEL_FIRST", "on") and not allow_no_model()


def _qa_requires_model(question: str) -> bool:
    """问答是否需要模型：越界问题由纯规则拒答（不需要模型），范围内问题要模型作答。

    口径与 `graph/nodes.py:boundary` 一致，只对"有自由文本"的情形做术语判定；
    没有文本时节点按"范围内"放行，但那种情况下检索拿不到条文、走的是
    "未检索到相关条文"的纯规则出口，照样不需要模型，所以这里直接返回 False。
    """
    text = (question or "").strip()
    if not text:
        return False
    return classify_by_terms(text).in_scope


def _deps(
    repo: Repository | None,
    *,
    max_ask_rounds: int | None = None,
    llm: LLMConfig | None = None,
) -> Deps:
    cfg = load_config()
    policy = cfg.policy if max_ask_rounds is None else replace(cfg.policy, max_ask_rounds=max_ask_rounds)
    # 未显式传入就交给统一 resolver（环境变量 > auth.json > 未配置）；旧库 Key 已由引导阶段迁移。
    # 显式传入的路径保持原语义：只有 api_key 非空才构造生成器。
    llm_cfg = llm if llm is not None else resolve_llm_config()[0]
    generator = DeepSeekLLM(llm_cfg) if llm_cfg is not None and llm_cfg.api_key else None
    extractor: Extractor
    mode = cfg.extraction.mode
    if mode == "regex":
        # 显式正则档：绝不调用模型，也不产生降级文案
        extractor = RegexExtractor()
    elif mode == "llm":
        # 显式模型档：没有 Key 也交给 LLMExtractor，由它自己标降级并给出「未配置 Key」诊断
        extractor = LLMExtractor(generator)
    elif generator is not None:
        # 与处理建议共用同一个 DeepSeekLLM 实例，token 才能合并计数
        extractor = LLMExtractor(generator, fallback=RegexExtractor())
    else:
        extractor = RegexExtractor()
    rules = load_rules(cfg.paths.rules)
    return Deps(
        rules=rules,
        retriever=build_retriever(cfg),
        generator=generator,
        extractor=extractor,
        repo=repo,
        policy=policy,
    )


def run_check(
    *,
    user: User,
    contract_text: str = "",
    facts: dict[str, str] | None = None,
    enterprise_id: str = "",
    repo: Repository | None = None,
    variant: str = "full",
    ask_rounds: int = 0,
    max_ask_rounds: int | None = None,
    save: bool = True,
    contract_name: str = "",
    contract_id: str = "",
    llm: LLMConfig | None = None,
    clause_review: bool | None = None,
) -> CheckTask:
    """核对模式：跑图 → 组装任务 → 落库 + 日志。

    `ask_rounds` 用于「追问 → 用户作答 → 重跑」：第二次调用时把它设成已问过的轮数，
    这样内核不会在同一轮里反复自问；`save=False` 时只算不落库（首轮试探性追问用）。
    `clause_review` 是界面开关的显式覆盖：None 走配置（`MLA_CLAUSE_REVIEW`），
    True / False 由调用方（界面勾选框）决定，默认不改变既有行为。

    门禁：模型是核对的大脑，没有可用模型就拒绝执行（抛 `NoModelError`），
    不跑图、不落库、不写日志；`MLA_ALLOW_NO_LLM=1` 时放行，走与改造前一致的旧路径。
    """
    if not allow_no_model() and not has_model(llm):
        raise NoModelError(NO_MODEL_MESSAGE)
    init: CheckState = {
        "task_id": "T-" + uuid.uuid4().hex[:10],
        "mode": TaskMode.CHECK.value,
        "facts": facts or {},
        "contract_text": contract_text,
        "variant": variant,
        "ask_rounds": ask_rounds,
    }
    deps = _deps(repo, max_ask_rounds=max_ask_rounds, llm=llm)
    return _run(
        init,
        user=user,
        enterprise_id=enterprise_id,
        repo=repo,
        deps=deps,
        save=save,
        contract_name=contract_name,
        contract_chars=len(contract_text),
        contract_id=contract_id,
        clause_review=clause_review,
    )


def run_qa(
    *,
    user: User,
    question: str,
    enterprise_id: str = "",
    repo: Repository | None = None,
    llm: LLMConfig | None = None,
    context: dict | None = None,
) -> CheckTask:
    """受限问答模式：同一管线，入口换成问题。

    `context` 是调用方给的结构化核对摘要（事实 / 规则命中 / 条款结论），
    只用于拼提示词，不进检索查询；不传时行为与改造前一致。

    门禁（与 `run_check` 同理，但保留既有的"拒答结果"语义）：越界问题由纯规则拒答，
    根本不需要模型，照旧走完流程并返回拒答任务；**范围内的问题要模型作答**，
    这时拿不到模型就抛 `NoModelError`，不再产出"未配置模型"的残缺任务。
    """
    if not allow_no_model() and not has_model(llm) and _qa_requires_model(question):
        raise NoModelError(NO_MODEL_QA_MESSAGE)
    init: CheckState = {
        "task_id": "T-" + uuid.uuid4().hex[:10],
        "mode": TaskMode.QA.value,
        "question": question,
        "facts": {},
        "contract_text": "",
        "variant": "full",
        "qa_context": dict(context or {}),
    }
    return _run(
        init,
        user=user,
        enterprise_id=enterprise_id,
        repo=repo,
        deps=_deps(repo, llm=llm),
        save=True,
    )


def _review_clauses(
    init: CheckState,
    deps: Deps,
    clause_review: bool | None = None,
    *,
    rule_checks: Sequence[RuleCheck] | None = None,
    facts: Mapping[str, Any] | None = None,
) -> tuple[list[ClauseFinding], list[str], bool, RetryStats]:
    """条款审查（存在性域）：仅启用且有模型时执行；失败不阻塞主流程。

    `clause_review` 为 None 时按配置（`MLA_CLAUSE_REVIEW`），否则以调用方开关为准。
    第三个返回值是「本次是否真的跑了审查」——落库后历史详情据此区分「未做」与「未发现」；
    未配置模型 / 触发器读不出 / 没有合同正文都算没跑（False），命中不了候选算跑过（True）。
    `rule_checks` / `facts` 来自图跑完后的最终状态，随 payload 下发给模型作判断参考
    （规格 003 第 13 章 P2-3）；不传时 payload 是空列表 / 空对象，行为与改造前一致。
    第四个返回值是 L4 返工的用量（规格 003 第 4.5.5 节）：只供任务落库与评测，界面不展示。
    """
    cfg = load_config()
    if not (cfg.clause_review.enabled if clause_review is None else clause_review):
        return [], [], False, RetryStats()
    contract_text = str(init.get("contract_text") or "")
    if not contract_text.strip():
        return [], [], False, RetryStats()
    if deps.generator is None:
        return [], ["条款审查未启用：未配置大模型"], False, RetryStats()
    try:
        triggers = load_triggers(cfg.paths.clauses)
    except (OSError, ValueError) as exc:
        return [], [f"条款审查未启用：触发器表读取失败（{exc}）"], False, RetryStats()
    candidates = detect_clauses(contract_text, triggers)
    if not candidates:
        return [], ["条款审查：未命中可疑条款候选"], True, RetryStats()
    errors: list[str] = []
    retry_stats = RetryStats()
    findings = review_clauses(
        deps.generator,
        candidates,
        contract_text,
        batch_size=cfg.clause_review.batch_size,
        max_extra=cfg.clause_review.max_extra,
        errors=errors,
        rule_checks=rule_checks,
        facts=facts,
        retry_stats=retry_stats,
    )
    return list(findings), errors, True, retry_stats


def _dedupe_demoted_rule(items: list[CheckItem], findings: list[ClauseFinding]) -> list[CheckItem]:
    """条款审查已就同一问题给出结论时，剔除让位的规则条目（避免同一风险报两次）。

    规则本身始终参与判定：条款通道为空（未启用 / 模型失败 / 未命中触发器）时原样返回，
    这条最清晰的风险必须继续由规则报出来。
    """
    if not any(str(f.trigger_id) == PROBATION_WAGE_TRIGGER_ID for f in findings):
        return items
    return [item for item in items if item.rule_id != PROBATION_WAGE_RULE_ID]


def _run(
    init: CheckState,
    *,
    user: User,
    enterprise_id: str,
    repo: Repository | None,
    deps: Deps,
    save: bool = True,
    contract_name: str = "",
    contract_chars: int = 0,
    contract_id: str = "",
    clause_review: bool | None = None,
) -> CheckTask:
    started = time.perf_counter()
    final = build_graph(deps).invoke(init)
    # 条款审查要看到图跑完后的最终校验信号与事实梳理：只在此时它们才是完整的
    # （`extract` 已把正则/模型抽取的事实并进 `facts`，`rule_check` 已产出 `rule_checks`）。
    clause_findings, clause_errors, review_ran, retry_stats = _review_clauses(
        init,
        deps,
        clause_review,
        rule_checks=list(final.get("rule_checks") or []),
        facts=dict(final.get("facts") or {}),
    )
    # 路径判定只看"本次是不是核对 + 开关"：问答任务 items 恒为空，不走共识层，也不写这个标志。
    model_first = model_first_enabled() and str(init.get("mode") or "") == TaskMode.CHECK.value
    if model_first:
        # 共识层节点（P1-3）：条款审查之后、写库之前，由它决定最终 items。
        # 规则通道的违规条目到此为止不再进 items（仍在 rule_checks / rule_coverage 留痕）。
        consensus_state = consensus(
            {
                "clause_findings": list(clause_findings),
                "rule_checks": list(final.get("rule_checks") or []),
            },
            deps,
        )
        items = _dedupe_demoted_rule(list(consensus_state.get("items") or []), clause_findings)
        # 分级诊断（如模型影响度建议与要素值不一致）与条款审查诊断同口径落库，事后可审计。
        clause_errors = clause_errors + [str(e) for e in (consensus_state.get("errors") or [])]
    else:
        # 旧路径不变：items = 规则条目，让位改成事后去重，写库前完成。
        items = _dedupe_demoted_rule(list(final.get("items") or []), clause_findings)
    latency = time.perf_counter() - started

    # 记录本次实际使用的模型名（历史记录要能回答"这次用的哪个模型"）：
    # 未配置模型（没有生成器）时写 "none" 而不是空串，避免与"旧库没这一列"混为一谈。
    model_name = "none"
    if deps.generator is not None:
        model_name = str(getattr(getattr(deps.generator, "cfg", None), "model", "") or "none")

    task = CheckTask(
        task_id=str(init.get("task_id") or ""),
        mode=TaskMode(str(init.get("mode") or "核对")),
        enterprise_id=enterprise_id or user.enterprise_id,
        variant=str(init.get("variant") or "full"),
        model=model_name,
        facts=dict(final.get("facts") or {}),
        items=items,
        suggestions=list(final.get("suggestions") or []),
        case_refs=list(final.get("case_refs") or []),
        ask_history=list(final.get("ask_history") or []),
        ask_rounds=int(final.get("ask_rounds") or 0),
        questions=[str(q) for q in (final.get("questions") or [])],
        ask_items=[dict(x) for x in (final.get("ask_items") or []) if isinstance(x, dict)],
        refused=bool(final.get("refused")),
        boundary_layer=str(final.get("boundary_layer") or ""),
        boundary_reason=str(final.get("boundary_reason") or ""),
        verify_ok=bool(final.get("verify_ok", True)),
        degraded=bool(final.get("degraded")),
        errors=[str(e) for e in (final.get("errors") or [])] + clause_errors,
        agent_trace=[str(x) for x in (final.get("agent_trace") or [])],
        clause_review=review_ran,
        model_first=model_first,
        clause_findings=clause_findings,
        clause_retry_calls=retry_stats.calls,
        clause_retry_items=retry_stats.items,
        clause_retry_passed=retry_stats.passed,
        undetermined=list(final.get("undetermined") or []),
        unmet=list(final.get("unmet") or []),
        statutory_na=int(final.get("statutory_na") or 0),
        rule_coverage=final.get("rule_coverage"),
        rule_checks=list(final.get("rule_checks") or []),
        missing_terms=list(final.get("missing_terms") or []),
        token_in=int(getattr(deps.generator, "usage_in", 0) or 0),
        token_out=int(getattr(deps.generator, "usage_out", 0) or 0),
        latency_s=round(latency, 2),
        contract_name=contract_name,
        contract_chars=contract_chars,
        contract_id=contract_id,
        created_at=datetime.now(),
    )
    if repo is not None and save:
        persist(task, user=user, repo=repo, verify_ok=bool(final.get("verify_ok")))
    return task


def persist(task: CheckTask, *, user: User, repo: Repository, verify_ok: bool = True) -> None:
    """落库 + 写操作日志（界面在「跳过追问」时也要能补存首轮结果）。"""
    repo.save_task(task)
    repo.add_log(
        OperationLog(
            log_id="L-" + uuid.uuid4().hex[:10],
            task_id=task.task_id,
            user_id=user.user_id,
            action="核对" if task.mode is TaskMode.CHECK else "问答",
            detail=(
                f"条目 {len(task.items)} / 建议 {len(task.suggestions)} / "
                f"校验 {'通过' if verify_ok else '未通过'} / {task.latency_s or 0:.2f}s"
            ),
        )
    )
