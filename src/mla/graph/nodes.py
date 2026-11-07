"""状态图节点：输入 state，返回**增量**（不要原地改 state）。

依赖（规则表 / 检索器 / 生成器 / 仓储 / 策略）通过 `Deps` 注入，节点不自己 new 实现，
这样单测可以传假实现，不联网也能跑全链路。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ..adapters.extract import RegexExtractor
from ..config import PolicyConfig, load_config
from ..domain.boundary import classify_by_terms
from ..domain.checklist import attach_cases
from ..domain.field_catalog import field_question
from ..domain.missing import detect_missing_terms, load_missing_terms
from ..domain.models import CheckItem, ItemKind, MissingTerm, RuleCoverage, UndeterminedRule
from ..domain.rules import RuleEntry, RuleOutcome, RuleStatus, evaluate, judge_all
from ..domain.slots import missing_slots
from ..domain.verify import verify as run_verify
from ..ports.extraction import ExtractionResult, Extractor
from ..ports.generation import Generator
from ..ports.repository import Repository
from ..ports.retrieval import Retriever
from ..services.asking import polish_questions
from ..services.drafting import draft_answer, draft_suggestions
from ..services.qa_agent import run_qa_agent
from .state import CheckState


@dataclass(frozen=True)
class Deps:
    """一次运行需要的依赖集合。"""

    rules: tuple[RuleEntry, ...]
    retriever: Retriever | None = None
    generator: Generator | None = None
    extractor: Extractor | None = None
    repo: Repository | None = None
    policy: PolicyConfig = field(default_factory=lambda: load_config().policy)


def intake(state: CheckState, deps: Deps) -> CheckState:
    """入口：补默认值，防止下游拿到 None。"""
    return {
        "mode": state.get("mode") or "核对",
        "facts": state.get("facts") or {},
        "variant": state.get("variant") or "full",
        "ask_rounds": state.get("ask_rounds") or 0,
        "ask_history": list(state.get("ask_history") or []),
        "errors": [],
    }


def boundary(state: CheckState, deps: Deps) -> CheckState:
    """越界判定（三层）：术语定向 → 检索兜底（节点内）→ 默认保守拒答。"""
    text = " ".join(filter(None, [state.get("question", ""), state.get("contract_text", "")])).strip()
    if not text:
        # 只有结构化事实、没有自由文本：属于"纯规则核对"，视为范围内
        return {"in_scope": True, "boundary_layer": "facts"}
    decision = classify_by_terms(text)
    return {
        "in_scope": decision.in_scope,
        "boundary_layer": decision.layer,
        "boundary_reason": decision.reason,
    }


def slots_check(state: CheckState, deps: Deps) -> CheckState:
    """必问事实是否齐全（**纯代码，零模型**）。

    缺阻断项且已达补问上限 → 标记降级（不硬猜），继续往下走。
    """
    facts = dict(state.get("facts") or {})
    missing = missing_slots(facts)
    ids = [s.slot_id for s in missing]
    rounds = int(state.get("ask_rounds") or 0)

    if missing and rounds >= deps.policy.max_ask_rounds:
        names = "、".join(s.name for s in missing)
        return {
            "missing_slots": ids,
            "degraded": True,
            "errors": list(state.get("errors") or []) + [f"补问 {rounds} 轮后仍缺：{names}"],
        }
    return {"missing_slots": ids}


def _aligned_asks(items: list[dict], missing_ids: list[str]) -> list[dict[str, str]]:
    """把追问清单对齐到本轮缺失的阻断项：顺序跟阻断项走，缺哪项就用固定问句补哪项。

    模型只被允许改写说法，不允许改变"问哪些字段"，所以这里的对齐是硬约束；
    复用上一轮结果时也走同一条路径，保证条数恒等于阻断项数。
    """
    by_slot = {str(item.get("slot_id") or ""): item for item in items if isinstance(item, dict)}
    out: list[dict[str, str]] = []
    for slot_id in missing_ids:
        item = by_slot.get(slot_id)
        if item is None:
            out.append({"slot_id": slot_id, "question": field_question(slot_id), "source": "fixed"})
            continue
        out.append(
            {
                "slot_id": slot_id,
                "question": str(item.get("question") or field_question(slot_id)),
                "source": str(item.get("source") or "fixed"),
            }
        )
    return out


def ask(state: CheckState, deps: Deps) -> CheckState:
    """生成追问并把轮数 +1（超限由 build.py 的条件边判定降级）。

    问句由模型润色，但**问哪些字段**仍由阻断项决定；只在本轮第一次追问时调用模型，
    后续轮次复用第一次的问句，避免同一批问题反复计费。轮数与路由语义不变。
    """
    missing_ids = list(state.get("missing_slots") or [])
    facts = dict(state.get("facts") or {})
    rounds_before = int(state.get("ask_rounds") or 0)
    rounds = rounds_before + 1
    errors = list(state.get("errors") or [])
    known = list(state.get("ask_items") or [])
    if rounds_before == 0 or not known:
        polished, ask_errors = polish_questions(deps.generator, facts, missing_ids, ask_round=rounds)
        ask_items = _aligned_asks(polished, missing_ids)
        errors.extend(ask_errors)
    else:
        ask_items = _aligned_asks(known, missing_ids)
    questions = [item["question"] for item in ask_items]
    history = list(state.get("ask_history") or [])
    history.append({"round": rounds, "questions": questions})
    return {
        "ask_rounds": rounds,
        "questions": questions,
        "ask_items": ask_items,
        "ask_history": history,
        "errors": errors + [f"第 {rounds} 轮补问：{len(questions)} 个问题"],
    }


def extract(state: CheckState, deps: Deps) -> CheckState:
    """合同关键项抽取：有合同文本就走抽取端口（默认正则），用户显式给的事实优先覆盖。"""
    contract_text = state.get("contract_text") or ""
    provided = {k: v for k, v in (state.get("facts") or {}).items() if str(v).strip()}
    outcome = ExtractionResult()
    if contract_text:
        outcome = (deps.extractor or RegexExtractor()).extract(contract_text)
    return {
        "extracted": outcome.facts,
        "facts": {**outcome.facts, **provided},
        "degraded": bool(state.get("degraded")) or outcome.degraded,
        "errors": list(state.get("errors") or []) + list(outcome.errors),
    }


def retrieve(state: CheckState, deps: Deps) -> CheckState:
    """法源检索：有检索器就走 BM25（已做时效过滤）；没有则返回空。

    三档消融：只有 full 档走检索（llm_only / llm_rules 关闭）。
    """
    if (state.get("variant") or "full") in ("llm_only", "rules_only"):
        return {"retrieved": []}
    if deps.retriever is None:
        return {"retrieved": []}
    query = state.get("question") or state.get("contract_text") or ""
    if not query.strip():
        return {"retrieved": []}
    facts = {k: v for k, v in (state.get("facts") or {}).items() if str(v).strip()}
    # 语义路只用问题或结构化事实，避免把合同正文发往云端
    dense_query = str(state.get("question") or "") or "；".join(f"{k}：{v}" for k, v in facts.items())
    hits = deps.retriever.search(query, top_k=deps.policy.max_ask_rounds * 4, dense_query=dense_query)
    return {"retrieved": list(hits)}


def _undetermined_reason(outcome: RuleOutcome) -> str:
    """把"判不了"归到两类：缺前提字段，还是前提已满足但判定字段缺。

    只看缺的字段名落在哪一侧，不解析 `reason` 里的中文短语；
    `missing_fields` 为空（字段值或阈值无法解析）算判定字段问题——前提本身并没有缺。
    """
    premise = {outcome.rule.when_field, outcome.rule.when_field2} - {""}
    return "premise_missing" if premise & set(outcome.missing_fields) else "field_missing"


def rule_check(state: CheckState, deps: Deps) -> CheckState:
    """确定性规则判定：命中违规即产出风险条目（带法条 ID）。llm_only 档关闭规则。

    `items` 仍由 `evaluate` 的违规集合生成，条目顺序、字段、文案与旧口径逐字节一致；
    `undetermined` 与 `rule_coverage` 是旁路留痕，不参与 `items` 构造、不进 `items`。
    llm_only 档整层不跑，分布返回全零而非 None：下游统计不必再区分"没跑"与"跑了但全空"。
    """
    if (state.get("variant") or "full") == "llm_only":
        return {
            "rule_hits": [],
            "items": [],
            "undetermined": [],
            "rule_coverage": RuleCoverage(total=0, violated=0, compliant=0, not_applicable=0, unknown=0),
        }
    facts = dict(state.get("facts") or {})
    hits = evaluate(deps.rules, facts)
    items = [
        CheckItem(
            item_id=h.rule.rule_id,
            kind=ItemKind.RISK,
            clause_ref=str(state.get("extracted", {}).get("clause_ref", "")),
            finding=f"{h.rule.name}（实际 {h.value:g}{h.rule.unit}，限值 {h.rule.threshold}{h.rule.unit}）",
            source_id=h.rule.statute_id,
            severity=h.rule.severity,
            rule_id=h.rule.rule_id,
        )
        for h in hits
        if h.violated
    ]
    outcomes = judge_all(deps.rules, facts)
    undetermined = [
        UndeterminedRule(
            rule_id=o.rule.rule_id,
            name=o.rule.name,
            statute_id=o.rule.statute_id,
            missing_fields=o.missing_fields,
            reason=_undetermined_reason(o),
        )
        for o in outcomes
        if o.status is RuleStatus.UNKNOWN
    ]
    counts = Counter(o.status for o in outcomes)
    coverage = RuleCoverage(
        total=len(deps.rules),
        violated=counts[RuleStatus.VIOLATED],
        compliant=counts[RuleStatus.COMPLIANT],
        not_applicable=counts[RuleStatus.NOT_APPLICABLE],
        unknown=counts[RuleStatus.UNKNOWN],
    )
    return {
        "rule_hits": hits,
        "items": items,
        "undetermined": undetermined,
        "rule_coverage": coverage,
    }


def _source_statutes(state: CheckState, deps: Deps) -> list:
    """把本次用到的法条收集起来（规则命中 + 检索命中），供生成层引用。"""
    wanted = {i.source_id for i in (state.get("items") or []) if i.source_id}
    out: dict[str, object] = {}
    if deps.repo is not None:
        for sid in wanted:
            statute = deps.repo.get_statute(sid)
            if statute is not None:
                out[sid] = statute
    for statute in state.get("retrieved") or []:
        out.setdefault(statute.statute_id, statute)
    return list(out.values())


def checklist(state: CheckState, deps: Deps) -> CheckState:
    """汇总清单与建议：建议由大模型生成，必须带依据；没有模型就不生成。

    必备条款缺项（`MT-*`）只走旁路：读合同正文判"有没有写"，结果进 `missing_terms`，
    **绝不进 `items`**（`items` 只装规则风险，评测准确率按它算）。
    """
    items = list(state.get("items") or [])
    suggestions, errors = draft_suggestions(
        deps.generator, items, dict(state.get("facts") or {}), _source_statutes(state, deps)
    )
    cases = deps.repo.list_cases() if deps.repo is not None else []
    cfg = load_config()
    try:
        specs = load_missing_terms(cfg.paths.missing_terms)
    except (OSError, ValueError) as exc:
        missing: list[MissingTerm] = []
        errors = errors + [f"必备条款缺项未检测：规格表读取失败（{exc}）"]
    else:
        missing = list(
            detect_missing_terms(
                str(state.get("contract_text") or ""), specs, min_chars=cfg.missing_terms.min_chars
            )
        )
    return {
        "items": items,
        "suggestions": suggestions,
        "case_refs": attach_cases(items, cases),
        "missing_terms": missing,
        "errors": list(state.get("errors") or []) + errors,
    }


def verify_node(state: CheckState, deps: Deps) -> CheckState:
    """校验：结论/建议必须有依据。依据集合＝规则表法条与规则 ID + 本轮检索命中。"""
    allowed = {r.statute_id for r in deps.rules} | {r.rule_id for r in deps.rules}
    allowed |= {s.statute_id for s in (state.get("retrieved") or [])}
    report = run_verify(
        list(state.get("items") or []),
        list(state.get("suggestions") or []),
        allowed,
    )
    errors = list(state.get("errors") or []) + list(report.errors)
    degraded = bool(state.get("degraded")) or report.degraded
    return {"verify_ok": report.ok, "degraded": degraded, "errors": errors}


def refuse(state: CheckState, deps: Deps) -> CheckState:
    """拒答 + 转介：越界时不产出任何结论，只给转介建议。"""
    layer = state.get("boundary_layer") or "default"
    reason = state.get("boundary_reason") or ""
    return {
        "refused": True,
        "items": [],
        "suggestions": [],
        "case_refs": [],
        "verify_ok": True,
        "errors": [
            f"越界拒答（层：{layer}；理由：{reason}）；建议拨打 12348 公共法律服务热线或咨询专业律师。"
        ],
    }


def degrade(state: CheckState, deps: Deps) -> CheckState:
    """校验失败时的降级出口：保留结论，标记"依据不足"。"""
    return {"degraded": True, "errors": list(state.get("errors") or []) + ["已降级：部分结论依据不足"]}


def _qa_context_summary(context: dict) -> str:
    """把本次核对的结构化摘要压成纯文本，供问答提示词使用。

    只取事实值、规则命中与条款结论的段落号 / 等级；任何合同原文片段都不进这里，
    调用方传进来的 context 也不该含原文。
    """
    if not isinstance(context, dict) or not context:
        return ""
    lines: list[str] = []
    facts = context.get("facts")
    if isinstance(facts, dict) and facts:
        lines.append("结构化事实：")
        lines.extend(f"- {key}：{value}" for key, value in facts.items())
    items = [item for item in (context.get("items") or []) if isinstance(item, dict)]
    if items:
        lines.append("规则命中：")
        lines.extend(f"- {item.get('rule_id', '')}（依据 {item.get('statute_id', '')}）" for item in items)
    clauses = [clause for clause in (context.get("clauses") or []) if isinstance(clause, dict)]
    if clauses:
        lines.append("条款结论：")
        lines.extend(
            f"- 正文第 {clause.get('paragraph', '')} 段，等级 {clause.get('grade', '')}" for clause in clauses
        )
    return "\n".join(lines)


def qa_answer(state: CheckState, deps: Deps) -> CheckState:
    """受限问答出口：由大模型依据检索条文作答；没有模型 / 没有条文就拒答。

    输出落「处理建议」区（带免责声明），并交给校验器复查 source_id。
    带核对上下文时只把结构化摘要拼进给模型的提示词，检索查询仍是问题本身。
    """
    hits = list(state.get("retrieved") or [])
    if not hits:
        return {
            "items": [],
            "suggestions": [],
            "verify_ok": True,
            "errors": list(state.get("errors") or []) + ["未检索到相关条文：建议补充事实或转专业咨询"],
        }
    question = str(state.get("question") or "")
    summary = _qa_context_summary(state.get("qa_context") or {})
    prompt = f"{question}\n\n本次核对的结构化摘要（不含合同原文）：\n{summary}" if summary else question
    base_errors = list(state.get("errors") or [])
    if deps.generator is not None and hasattr(deps.generator, "chat"):
        suggestions, trace, errors = run_qa_agent(deps.generator, deps.retriever, deps.rules, prompt)
        if suggestions:
            return {
                "items": [],
                "suggestions": suggestions,
                "agent_trace": trace,
                "errors": base_errors + errors,
            }
        # 智能体失败 → 回退固定检索作答（不比改造前更差）
        fallback, fallback_errors = draft_answer(deps.generator, prompt, hits[:5])
        if fallback:
            return {
                "items": [],
                "suggestions": fallback,
                "agent_trace": trace + ["fallback:draft_answer"],
                "errors": base_errors + errors + fallback_errors,
            }
        reason = "；".join(errors + fallback_errors) or "模型未能依据条文作答"
        return {
            "items": [],
            "suggestions": [],
            "refused": True,
            "boundary_layer": "llm",
            "boundary_reason": reason,
            "verify_ok": True,
            "agent_trace": trace,
            "errors": base_errors + errors + fallback_errors,
        }

    suggestions, errors = draft_answer(deps.generator, prompt, hits[:5])
    if not suggestions:
        reason = "；".join(errors) or "未配置大模型或模型未能依据条文作答"
        return {
            "items": [],
            "suggestions": [],
            "refused": True,
            "boundary_layer": "llm",
            "boundary_reason": reason,
            "verify_ok": True,
            "errors": list(state.get("errors") or []) + errors,
        }
    return {"items": [], "suggestions": suggestions, "errors": list(state.get("errors") or []) + errors}


def output(state: CheckState, deps: Deps) -> CheckState:
    """出口：在此落库并写日志（token/耗时）。"""
    return {}
