"""任务编排服务：跑状态图、计时、落库、写操作日志（模块 4/5/6/10 的服务层）。

UI 与 CLI 都调这里，保证「同样的输入 → 同样的记录」。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from datetime import datetime

from ..adapters.extract import RegexExtractor
from ..adapters.extract_llm import LLMExtractor
from ..adapters.llm import DeepSeekLLM
from ..config import LLMConfig, load_config
from ..domain.clauses import detect_clauses, load_triggers
from ..domain.models import CheckItem, CheckTask, ClauseFinding, OperationLog, TaskMode, User
from ..domain.rules import load_rules
from ..graph.build import build_graph
from ..graph.nodes import Deps
from ..graph.state import CheckState
from ..ports.extraction import Extractor
from ..ports.repository import Repository
from .clause_review import review_clauses
from .retrieval import build_retriever

# 条款审查启用时，这条规则让位给触发器 TR-PROBATION-WAGE-BASE + 模型判定
# （规格 1.4：规则只看比例不看基数，会把"按基本工资 80%"误判为合规）。
# 让位是**事后去重**：规则始终参与判定，只有模型真的给出同域结论时才剔除规则条目；
# 模型失败 / 未启用 / 未命中触发器时规则照常报，避免这条最清晰的风险静默消失。
PROBATION_WAGE_RULE_ID = "R-PROBATION-WAGE-80"
PROBATION_WAGE_TRIGGER_ID = "TR-PROBATION-WAGE-BASE"


def _deps(
    repo: Repository | None,
    *,
    max_ask_rounds: int | None = None,
    llm: LLMConfig | None = None,
) -> Deps:
    cfg = load_config()
    policy = cfg.policy if max_ask_rounds is None else replace(cfg.policy, max_ask_rounds=max_ask_rounds)
    llm_cfg = llm or cfg.llm
    generator = DeepSeekLLM(llm_cfg) if llm_cfg.api_key else None
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
    """
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
    """
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
    init: CheckState, deps: Deps, clause_review: bool | None = None
) -> tuple[list[ClauseFinding], list[str], bool]:
    """条款审查（存在性域）：仅启用且有模型时执行；失败不阻塞主流程。

    `clause_review` 为 None 时按配置（`MLA_CLAUSE_REVIEW`），否则以调用方开关为准。
    第三个返回值是「本次是否真的跑了审查」——落库后历史详情据此区分「未做」与「未发现」；
    未配置模型 / 触发器读不出 / 没有合同正文都算没跑（False），命中不了候选算跑过（True）。
    """
    cfg = load_config()
    if not (cfg.clause_review.enabled if clause_review is None else clause_review):
        return [], [], False
    contract_text = str(init.get("contract_text") or "")
    if not contract_text.strip():
        return [], [], False
    if deps.generator is None:
        return [], ["条款审查未启用：未配置大模型"], False
    try:
        triggers = load_triggers(cfg.paths.clauses)
    except (OSError, ValueError) as exc:
        return [], [f"条款审查未启用：触发器表读取失败（{exc}）"], False
    candidates = detect_clauses(contract_text, triggers)
    if not candidates:
        return [], ["条款审查：未命中可疑条款候选"], True
    errors: list[str] = []
    findings = review_clauses(
        deps.generator,
        candidates,
        contract_text,
        batch_size=cfg.clause_review.batch_size,
        max_extra=cfg.clause_review.max_extra,
        errors=errors,
    )
    return list(findings), errors, True


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
    clause_findings, clause_errors, review_ran = _review_clauses(init, deps, clause_review)
    # 让位改成事后去重：写库前完成，统计 / 报告口径与界面一致。
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
        clause_findings=clause_findings,
        undetermined=list(final.get("undetermined") or []),
        unmet=list(final.get("unmet") or []),
        statutory_na=int(final.get("statutory_na") or 0),
        rule_coverage=final.get("rule_coverage"),
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
