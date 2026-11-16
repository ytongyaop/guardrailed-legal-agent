"""条款级模型判定（存在性域）：候选 → 批量判定 → 程序核验 → 分级结论。

流水线：

1. 候选由触发器层产出（`domain.clauses.detect_clauses`），本层**不做召回**；
2. 每 `batch_size`（默认 5）条候选一次模型调用，批量判定，避免 20 条串行把耗时拖到 100 秒以上；
3. `_verify` 逐条做程序核验：等级合法 → 证据长度 / 未跨段 / 落在候选所在段落（加严：不再退回首次出现）→
   法条在库（含 quote_statute 回声核验）→ 触发器条目引用在候选闭集内；A 级缺 quote_statute 降 B，
   A/B 级缺法条的降为 C（不丢弃），证据与候选片段重叠不足降一级（只降不升），其余硬性不过即丢弃并记诊断；
4. 模型可在候选之外自提发现（`extra`），**一律降为 C**、标 `source="model_extra"`、条数封顶。

消融开关：`verify=False` 时跳过第 3 步的四道校验，等级与法条 ID 原样保留（含库外条号），
供评测的 `model_raw` 臂度量"没有核验层会输出什么"；本条路径只用于度量，不用于生产展示。

隐私约束：发给模型的 `contract_paragraphs` **只包含候选条款及其邻近 ±2 行**，不发送合同全文
（姓名、住址、工资数等与审查无关的段落不出本机）。这是隐私优先的取舍：模型看不到全文，
它自己「额外发现」的问题会变少，不做补偿。

降级：没有 generator、调用失败、JSON 非法 → 空结果 + `errors`（不阻塞主流程，调用方据此标注未启用）。

prompt 与解析同住本模块（与 `services/drafting.py` 同口径）；内核结构 `ClauseFinding` 不带任何文案。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from typing import Any

from ..adapters.extract import _norm
from ..adapters.rag import load_statutes
from ..config import load_config
from ..domain.clauses import MAX_SPAN, ClauseCandidate
from ..domain.models import ClauseFinding, StatuteEntry
from ..domain.rules import RuleCheck
from ..ports.generation import Generator

GRADES = ("A", "B", "C")
# 单条法条原文注入上限（字符）：库内 143 条实测 max 567，故 600 足以整条注入；
# 只有超出上限时才截断并在 payload 标 `truncated`，让模型知道自己没看全。
STATUTE_FULL_MAX = 600
MAX_REASON = 200
MAX_FACTS = 5
MAX_FACT_CHARS = 80
MAX_LINE_CHARS = 160  # 单行送入 prompt 的字符上限（超长行截断，避免整段公文淹没候选）
# 证据（归一化后）长度硬区间：太短不足以证明是原文连续抄写，太长多半是整段照抄、
# 把候选之外的约定也带了进来。越界属不可修正（截断会改变证据语义），直接丢弃。
# 上限 100（原 80）的实测依据：`contract_traps_01` 被触发器命中的段 20 整行 = 84 字、
# 段 32 = 81 字，模型整行照抄是常见行为，80 会把这两处典型对抗条款误杀；101 字仍拦截。
MIN_EVIDENCE_CHARS = 10
MAX_EVIDENCE_CHARS = 100
OVERLAP_MIN = 0.5  # 模型证据与候选 span 的重叠下限（低于此值降一级 + 记诊断，span 仍取候选）

# 规格 003 第 13 章 P2-3：payload 每批下发 `rule_checks` 的条数上限。
# 只发有决策价值的两种状态（`mismatch` / `needs_fact`），规模远小于规则表本身；
# 留上限是防"规则表将来变大时 payload 无界增长"，截断必记诊断（模型据此知道自己没看全）。
# 数值 20 保留不动：规则表 16 条（003 第 5.3 节瘦身后）已低于上限，本项上限因此有余量。
MAX_RULE_CHECKS = 20
RULE_CHECK_STATUSES = ("mismatch", "needs_fact")

REVIEW_SYSTEM = """你是劳动合同条款风险审查助手。只输出 JSON 对象，不要解释，不要 Markdown。

输入四部分：contract_paragraphs（候选条款及其邻近段落，按段落编号）、candidates（本批待判定的可疑条款）、
rule_checks（程序按法条算出的数值校验结果）与 fact_sheet（用户已确认的用工事实）。
对每条候选判断：该约定与它给出的候选法条是否存在冲突，并按风险强度定级。

rule_checks 只列 mismatch（合同数值与法条不一致）与 needs_fact（缺事实、无法校验），它是程序的数值校验，
不是结论；fact_sheet 是用户自报的结构化事实，用来判断哪些条款已具备定级条件。二者只作判断参考，
不得直接复述为结论，也不得因为 rule_checks 标了 mismatch 就跳过自己的法条判断。

定级口径：
- A：候选法条明文禁止该约定，不需要补充事实即可判断；
- B：候选法条有相关规定，但需要补充事实（工资基数、岗位性质、审批备案情况等）或存在裁量空间；
- C：触发器命中但证据不足或语义不明确，无法定级。

硬性要求：
1. 每条候选只能从它自己的 candidate_statute_ids 里选法条，不得引用未列出的条号，不得编造条号。
2. reason 只写冲突点，句式写"该约定与 X 条某某规定不符/存在冲突"；不要写"该条款无效""该条款违法"这类终局结论。
3. evidence 必须从合同原文逐字连续抄写（10 到 100 字），不得改写、不得拼接、不得用省略号。
4. quote_statute 必须是所引法条原文的一段连续子串（原样摘抄，不改写、不概括、不加省略号）。
   A、B 级必填，抄与本次冲突最相关的那一段即可（对应 statute_ids 里第一顺位那条）；C 级可留空。
   条文原文里没有的句子不能写进来；拿不准原文就降为 C，不要编造原文。
5. A、B 级必须给法条 ID：只写 1 条最直接的依据（把最主要的那条放第一位）；
   确有两条并列且缺一不可时才写第二条，最多 2 条；
   不要罗列"可能相关"的条文，也不要为了兜底加引"合同无效"这类总则条款。
6. need_facts 填"还要补什么事实才能定论"，A 级可留空。
7. 候选之外若另有发现，放进 extra，最多 __MAX_EXTRA__ 条，grade 一律写 C。
8. 没有把握就写 C，不要猜。

输出格式：
{"findings":[{"idx":0,"grade":"A","reason":"...","statute_ids":["LCL-35"],"quote_statute":"...","evidence":"...","need_facts":[]}],"extra":[{"grade":"C","reason":"...","evidence":"...","statute_ids":[],"quote_statute":"","need_facts":[]}]}
"""


@lru_cache(maxsize=4)
def _norm_with_index(text: str) -> tuple[str, list[int]]:
    """归一化全文并记下每个字符在原文中的下标（口径同 `domain.clauses`，便于回声定位）。"""
    norm = _norm(text)
    index = [i for i, ch in enumerate(text) if not ch.isspace()]
    # 防御性分支，故意保留：`_norm` 的 `\s` 与 `isspace()` 空白口径一致，
    # 正常字符串输入构造不出长度不一致；保留是为了「口径将来被改动」时直接报错，
    # 而不是静默按错位的下标切 span。删掉它只会让这类改动变成隐性数据错误。
    if len(index) != len(norm):
        raise ValueError("归一化文本与原文下标长度不一致")
    return norm, index


def _locate(evidence: str, contract_text: str, paragraph: int | None = None) -> tuple[int, int] | None:
    """把模型证据（原文抄写，可能带空格差异）映射回合同原文的起止下标；定位不到返回 None。

    同一段文字可能在合同里出现多次（模板条款、重复样板）。给了 `paragraph` 时优先取落在该段
    的那一次，避免把第二处的证据绑到第一处；找不到同段命中才退回首次出现。

    **退回首次出现只给消融臂（`_unverified`）用**：生产核验路径（`_verify`）另加
    「必须落在候选所在段落」的硬校验，别段的那次一律不采信（P2-2 加严）。
    """
    norm_evidence = _norm(evidence)
    if not norm_evidence:
        return None
    norm_text, index = _norm_with_index(contract_text)
    first: tuple[int, int] | None = None
    pos = norm_text.find(norm_evidence)
    while pos >= 0:
        located = (index[pos], index[pos + len(norm_evidence) - 1] + 1)
        if first is None:
            first = located
        if paragraph is None or _paragraph_of(contract_text, located[0]) == paragraph:
            return located
        pos = norm_text.find(norm_evidence, pos + 1)
    return first


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（span 都不长，滚动数组足够）。"""
    if not a or not b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    best = 0
    prev = [0] * (len(b) + 1)
    for ch in a:
        cur = [0] * (len(b) + 1)
        for j, other in enumerate(b, start=1):
            if ch == other:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _overlap_ratio(a: str, b: str) -> float:
    """归一化后的重叠比例（分母取较短一方，与触发器校验器同口径）。"""
    na, nb = _norm(a), _norm(b)
    shortest = min(len(na), len(nb))
    if not shortest:
        return 0.0
    return _lcs_len(na, nb) / shortest


# 自提只用于「触发器完全没捞到的段落」。同一句里的多个问题由触发器按事由分别召回
# （triggers.yaml 一条触发器对应一类问题），不靠自提拆分；否则同一句会被重复报 2–3 次。
def _is_duplicate(finding: ClauseFinding, existing: Sequence[ClauseFinding]) -> bool:
    """自提条目是否与已保留结论重叠：归一化后重叠 ≥ `OVERLAP_MIN` 即视为重复，不看事由。

    实测依据（2026-11-06）：按事由保留后，8 条自提全部与既有结论同句、同一问题，
    只放大噪音；跨事由的拆分交回触发器层。
    """
    return any(_overlap_ratio(finding.span, other.span) >= OVERLAP_MIN for other in existing)


def _paragraph_of(text: str, pos: int) -> int:
    """字符下标所属段落号（按非空行计，与 `domain.clauses` 的 paragraph 口径一致）。"""
    paragraph = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.strip():
            paragraph += 1
        if offset + len(line) > pos:
            return max(paragraph, 1)
        offset += len(line)
    return max(paragraph, 1)


@lru_cache(maxsize=1)
def _statute_library() -> dict[str, StatuteEntry]:
    """法源库（143 条）：只读一次，供 prompt 摘录与闭集核验共用。"""
    rows = load_statutes(load_config().paths.statutes)
    return {s.statute_id: s for s in rows}


def _quote_in_statute(quote: str, statute_text: str) -> bool:
    """模型抄的法条原文是否真在该条 `text` 里（归一化后为连续子串）。

    归一化只消除**空白差异**：`_norm` 删掉全部空白字符（含换行），比"连续空白折叠为单空格"
    更宽，但同样只动空白——与 `_locate` / `_overlap_ratio` 是同一口径，避免出现两套空白标准。
    **不做**同义改写容错、不做模糊匹配：形似但不存在的仿写必须被拦住，否则回声核验没有意义。
    """
    norm_quote = _norm(quote)
    if not norm_quote:
        return False
    return norm_quote in _norm(statute_text)


def _statutes_payload(
    candidate: ClauseCandidate, library: Mapping[str, StatuteEntry]
) -> list[dict[str, Any]]:
    """候选法条全文（单条上限 `STATUTE_FULL_MAX`），超出上限才标 `truncated`。

    旧口径只发前 200 字摘录，会把关键的但书 / 第二款截掉，模型据此判冲突容易判错；
    改为整条注入后，`quote_statute` 才能逐字抄到"冲突点所在的那一句"。
    """
    out: list[dict[str, Any]] = []
    for sid in candidate.candidate_statute_ids:
        entry = library.get(sid)
        if entry is None:  # 触发器表已校验，这里只做防御：库缺条目时不塞空壳
            continue
        text = entry.text or ""
        out.append(
            {
                "statute_id": sid,
                "law": entry.law_name,
                "article": entry.article_no,
                "text": text[:STATUTE_FULL_MAX],
                "truncated": len(text) > STATUTE_FULL_MAX,
            }
        )
    return out


def _outline(contract_text: str, focus: set[int]) -> list[str]:
    """只输出候选条款及其邻近段落（±2 行），不发送合同全文。

    隐私约束（P0-3）：`focus` 是候选所在段落号集合，函数**始终**过滤，不按长度分支——
    短合同以前会整份外发（含姓名、住址、工资数等与审查无关的段落），那是缺陷不是优化。
    `focus` 为空（没有候选）时过滤结果自然为空列表：此时也不会发起审查。
    """
    lines = [(i, line.strip()) for i, line in enumerate(_non_empty_lines(contract_text), start=1)]
    lines = [(p, line) for p, line in lines if any(abs(p - f) <= 2 for f in focus)]
    return [f"{p}. {line[:MAX_LINE_CHARS]}" for p, line in lines]


def _non_empty_lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _rule_checks_payload(
    checks: Sequence[RuleCheck] | None,
    allowed_statutes: set[str] | None = None,
    diagnostics: list[str] | None = None,
) -> list[dict[str, str]]:
    """把校验信号压成 payload 形态：只发**与本批候选相关**且有决策价值的那几条。

    口径（规格 003 第 6.1 / 第 13 章 P2-3）：
    - **按候选相关法条过滤**：只留 `statute_id` 落在本批候选 `candidate_statute_ids` 并集里的
      规则（`allowed_statutes`）。事实稀疏时规则表里绝大多数是 `needs_fact`，不过滤等于把全表
      下发给每一批，既撑大 payload 又用无关规则的阈值干扰模型；`allowed_statutes=None` 表示不按
      法条筛（只在直接调用 / 单测里用，生产路径一律传本批并集）。
    - `ok` 不发：那是"这条数值没问题"，对判定没有增量，只会稀释模型注意力；
    - 字段只留 `rule_id` / `status` / `field` / `expected` / `actual`；`evidence_span` 与
      `statute_id` 不发——法条原文已在候选的 `statutes` 段里，重复一遍是噪音；
    - 超过 `MAX_RULE_CHECKS` 截断并记诊断；过滤后每批的条数不同，同一句话只记一条
      （否则 5 批会刷出 5 条重复诊断）。
    """
    rows: list[dict[str, str]] = []
    for check in checks or ():
        status = str(check.status)
        if status not in RULE_CHECK_STATUSES:
            continue
        if allowed_statutes is not None and check.statute_id not in allowed_statutes:
            continue
        rows.append(
            {
                "rule_id": check.rule_id,
                "status": status,
                "field": check.field,
                "expected": check.expected,
                "actual": check.actual,
            }
        )
    if len(rows) > MAX_RULE_CHECKS:
        if diagnostics is not None:
            message = (
                f"rule_checks 按本批候选相关法条过滤后仍有 {len(rows)} 条，"
                f"超出下发上限 {MAX_RULE_CHECKS} 条，已截断"
            )
            if message not in diagnostics:
                diagnostics.append(message)
        rows = rows[:MAX_RULE_CHECKS]
    return rows


def _fact_sheet(facts: Mapping[str, Any] | None, priority_fields: Sequence[str] = ()) -> dict[str, str]:
    """把用户事实压成 payload 形态：只发已填的结构化事实，空值不发。

    口径（规格 003 第 13 章 P2-3）：键名保持字段 id（不翻译，模型能从 checklist 看到中文说明）；
    条数与单值长度沿用 `MAX_FACTS` / `MAX_FACT_CHARS`（与 `need_facts` 同一口径），
    只收 `facts` 这类"字段 id → 短值"的结构化字典，不收任何自由文本。

    **优先级**：`priority_fields`（本批 `rule_checks` 涉及的字段）先入选——这些字段直接参与数值
    校验，被 `MAX_FACTS` 截掉会让模型只看到"缺事实"却看不到"缺哪一个"；其余已填事实按 `facts`
    原顺序补齐。按插入顺序硬截断会把 `probation_wage_ratio` / `written_contract` 这类排在后位的
    关键事实丢掉。
    """
    filled: dict[str, str] = {}
    for key, value in (facts or {}).items():
        text = str(value).strip()
        if text:
            filled[str(key)] = text

    ordered: list[str] = []
    for field in priority_fields:
        if field in filled and field not in ordered:
            ordered.append(field)
    ordered.extend(key for key in filled if key not in ordered)

    return {key: filled[key][:MAX_FACT_CHARS] for key in ordered[:MAX_FACTS]}


def _payload(
    batch: Sequence[ClauseCandidate],
    contract_text: str,
    library: Mapping[str, StatuteEntry],
    max_extra: int,
    *,
    rule_checks: Sequence[RuleCheck] | None = None,
    facts: Mapping[str, Any] | None = None,
    diagnostics: list[str] | None = None,
) -> dict[str, Any]:
    """单批输入。隐私口径不变：正文只发候选 ±2 行（`_outline`），不新增任何合同片段。

    `rule_checks` / `fact_sheet` 是结构化数据（校验信号 + 用户事实），**每批一份**放在顶层，
    不挂到单条候选上：同一条规则可能与多条候选相关，按候选重复会放大体积。

    两者都**按本批候选**算：并集取本批所有候选的 `candidate_statute_ids`（每批候选不同，
    不能全局算一次），只有引这些法条的规则才下发；`fact_sheet` 再按这些规则的 `field` 排序，
    保证参与数值校验的字段不被 `MAX_FACTS` 截掉。
    """
    focus = {c.paragraph for c in batch}
    allowed_statutes = {sid for candidate in batch for sid in candidate.candidate_statute_ids}
    rule_rows = _rule_checks_payload(rule_checks, allowed_statutes, diagnostics)
    fact_sheet = _fact_sheet(facts, [row["field"] for row in rule_rows])

    candidates: list[dict[str, Any]] = []
    for idx, candidate in enumerate(batch):
        candidates.append(
            {
                "idx": idx,
                "paragraph": candidate.paragraph,
                "span": candidate.span,
                "trigger_id": candidate.trigger_id,
                "name": candidate.name,
                "severity_hint": candidate.severity_hint,
                "candidate_statute_ids": list(candidate.candidate_statute_ids),
                "statutes": _statutes_payload(candidate, library),
            }
        )
    return {
        "contract_paragraphs": _outline(contract_text, focus),
        "candidates": candidates,
        "rule_checks": rule_rows,
        "fact_sheet": fact_sheet,
        "max_extra": max(0, int(max_extra)),
    }


def _grade_rank(grade: str) -> int:
    """排序用等级序号：未知等级排在 A/B/C 之后（关闭核验时模型可能给出非法等级）。"""
    return GRADES.index(grade) if grade in GRADES else len(GRADES)


def _unverified(
    finding_raw: dict[str, Any],
    candidate: ClauseCandidate | None,
    contract_text: str,
) -> ClauseFinding:
    """关闭核验时的输出映射（消融用）：等级与法条原样保留，只做定位与长度截断。

    - 等级不合法也原样保留（不降级、不封顶），供 `model_raw` 臂度量模型的真实输出；
    - 法条 ID 不过库、不过候选集（库外条号留给评测单列统计）；
    - 证据能在原文定位就取原文切片（保证 span 是原文子串），定位不到就保留模型原文。
    """
    raw_grade = str(finding_raw.get("grade", "") or "").strip().upper()
    evidence = str(finding_raw.get("evidence", "") or "").strip()
    located = (
        _locate(evidence, contract_text, candidate.paragraph if candidate is not None else None)
        if evidence
        else None
    )
    if located is not None:
        start, end = located
        span = contract_text[start : min(end, start + MAX_SPAN)]
        paragraph = candidate.paragraph if candidate is not None else _paragraph_of(contract_text, start)
    else:
        span = evidence[: MAX_SPAN * 4]
        paragraph = candidate.paragraph if candidate is not None else 0

    raw_ids = finding_raw.get("statute_ids") or []
    ids = tuple(
        dict.fromkeys(
            str(x).strip() for x in (raw_ids if isinstance(raw_ids, list) else []) if str(x).strip()
        )
    )
    reason = str(finding_raw.get("reason", "") or "").strip()[:MAX_REASON]
    quote = str(finding_raw.get("quote_statute", "") or "").strip()
    raw_facts = finding_raw.get("need_facts") or []
    facts = tuple(
        str(x).strip()[:MAX_FACT_CHARS]
        for x in (raw_facts if isinstance(raw_facts, list) else [])
        if str(x).strip()
    )[:MAX_FACTS]

    return ClauseFinding(
        paragraph=paragraph,
        span=span,
        grade=raw_grade,
        reason=reason or "（关闭核验：模型未给理由）",
        statute_ids=ids,
        trigger_id=candidate.trigger_id if candidate is not None else "",
        source="trigger" if candidate is not None else "model_extra",
        need_facts=facts,
        # 消融臂原样保留模型抄的法条原文（不清空、不降级），供评测度量"没有核验层会输出什么"
        quote_statute=quote,
    )


def _verify(
    finding_raw: dict[str, Any],
    candidate: ClauseCandidate | None,
    contract_text: str,
    library: Mapping[str, StatuteEntry],
    *,
    errors: list[str] | None = None,
    verify: bool = True,
) -> ClauseFinding | None:
    """单条程序核验：任一硬性不过返回 None（丢弃）；软性问题降级或只记诊断。

    硬性（丢弃）：等级不是 A/B/C；证据长度不在 10–100 字；证据含换行 / 跨段落边界；
    证据不在候选所在段落（不再退回首次出现）；引用法条不在库；触发器条目引用越出候选闭集。
    软性（保留）：A/B 级缺法条 → 降为 C；A 级缺 quote_statute（第 1 道三件套）→ 降为 B；
    quote_statute 不是所引法条原文的连续子串（第 3 道回声核验）→ 清空该引用，A 级随之降 B；
    模型证据与候选片段重叠不足 → 降一级（只降不升），span 仍取候选片段。
    `verify=False` 直接走 `_unverified`（消融的 model_raw 臂），不做任何校验。
    """
    if not verify:
        return _unverified(finding_raw, candidate, contract_text)

    diagnostics = errors if errors is not None else []
    tag = candidate.trigger_id if candidate is not None else "extra"
    raw_grade = str(finding_raw.get("grade", "") or "").strip().upper()
    if raw_grade not in GRADES:
        diagnostics.append(f"{tag}：等级不合法（{raw_grade or '空'}），已丢弃")
        return None

    evidence = str(finding_raw.get("evidence", "") or "").strip()
    # 第 2 道（加严 1/3）：长度硬校验。用归一化长度（空白差异不该决定是否越界）。
    norm_evidence = _norm(evidence)
    if not MIN_EVIDENCE_CHARS <= len(norm_evidence) <= MAX_EVIDENCE_CHARS:
        diagnostics.append(
            f"{tag}：第 2 道核验：证据长度越界（归一化后 {len(norm_evidence)} 字，"
            f"要求 {MIN_EVIDENCE_CHARS}–{MAX_EVIDENCE_CHARS} 字），已丢弃"
        )
        return None
    # 第 2 道（加严 2/3）：跨段拼接。证据体里出现换行就是两段拼起来的，不是连续抄写。
    if "\n" in evidence or "\r" in evidence:
        diagnostics.append(f"{tag}：第 2 道核验：证据跨段拼接（含换行），已丢弃")
        return None
    located = _locate(evidence, contract_text, candidate.paragraph if candidate is not None else None)
    if located is None:
        # 多数情况是编造原文；但"只在别的段落出现"要单独成一种原因，便于事后统计看错条款的比例。
        if candidate is not None and _locate(evidence, contract_text, None) is not None:
            diagnostics.append(
                f"{tag}：第 2 道核验：证据只在候选第 {candidate.paragraph} 段之外的段落出现"
                f"（不再退回首次出现），已丢弃"
            )
            return None
        diagnostics.append(f"{tag}：证据无法在合同原文定位，已丢弃")
        return None
    if candidate is not None:
        # 第 2 道（加严 3/3）：必须落在候选所在段落内。先核起点（起点在别段＝模型看错了条款），
        # 再核尾巴：归一化会删掉空白，因此"无换行"也可能把上一段结尾和下一段开头拼成一个匹配。
        if _paragraph_of(contract_text, located[0]) != candidate.paragraph:
            diagnostics.append(
                f"{tag}：第 2 道核验：证据不在候选所在段落（候选在第 {candidate.paragraph} 段），已丢弃"
            )
            return None
        if _paragraph_of(contract_text, located[1] - 1) != candidate.paragraph:
            diagnostics.append(
                f"{tag}：第 2 道核验：证据跨段拼接（归一化后跨过第 {candidate.paragraph} 段边界），已丢弃"
            )
            return None

    raw_ids = finding_raw.get("statute_ids") or []
    if not isinstance(raw_ids, list):
        diagnostics.append(f"{tag}：statute_ids 不是列表，已丢弃")
        return None
    ids = tuple(dict.fromkeys(str(x).strip() for x in raw_ids if str(x).strip()))
    unknown = [sid for sid in ids if sid not in library]
    if unknown:
        diagnostics.append(f"{tag}：引用了法源库中不存在的条号 {' / '.join(unknown)}，已丢弃")
        return None
    if candidate is not None:
        allowed = set(candidate.candidate_statute_ids)
        outside = [sid for sid in ids if sid not in allowed]
        if outside:
            diagnostics.append(f"{tag}：引用越出候选闭集 {' / '.join(outside)}，已丢弃")
            return None

    # 第 3 道（加严）：quote_statute 必须是所引法条 text 归一化后的连续子串。
    # 抄错 / 仿写只清空引用、不丢整条——结论的证据已在第 2 道核验过在原文可定位，
    # 硬性丢弃留给"证据定位不到"这类不可修正的失败。
    quote = str(finding_raw.get("quote_statute", "") or "").strip()
    if quote and not ids:
        diagnostics.append(f"{tag}：第 3 道核验：quote_statute 无对应法条（statute_ids 为空），已清空该引用")
        quote = ""
    elif quote and not any(_quote_in_statute(quote, library[sid].text or "") for sid in ids):
        diagnostics.append(
            f"{tag}：第 3 道核验：quote_statute 不是所引法条（{' / '.join(ids)}）原文的连续子串，已清空该引用"
        )
        quote = ""

    grade = raw_grade if candidate is not None else "C"  # 模型自提一律封顶 C，不采信其等级
    if grade in ("A", "B") and not ids:
        diagnostics.append(f"{tag}：{grade} 级没有法条依据，已降为 C")
        grade = "C"
    # 第 1 道（加严）：A 级要求 statute_ids + evidence + quote_statute 三件套。
    # evidence 已在第 2 道核验可定位、ids 已过库与闭集，故此处能触发缺件的只有 quote_statute；
    # 缺则降 B（逐级降，不丢整条）。B 级缺 quote_statute 沿用现规则：保留 B、不降级
    # （P2-2 的 L3.2 行为校验才会对 B 级缺失判返工）。
    if grade == "A" and not quote:
        diagnostics.append(f"{tag}：第 1 道核验：A 级缺 quote_statute（法条原文摘录），已降为 B")
        grade = "B"

    start, end = located
    if candidate is not None:
        paragraph = candidate.paragraph
        span = candidate.span
        ratio = _overlap_ratio(evidence, span)
        if ratio < OVERLAP_MIN:
            # 第 6 道（加严）：重叠不足说明模型可能看错了条款 → 降一级，只降不升。
            # span 仍一律取候选（证据只看作对照，不改写定位），已是 C 就保持 C 但仍留诊断。
            lowered = GRADES[min(GRADES.index(grade) + 1, len(GRADES) - 1)]
            if lowered == grade:
                diagnostics.append(
                    f"{tag}：第 6 道核验：模型证据与候选片段重叠不足"
                    f"（{ratio:.2f} < {OVERLAP_MIN}），已是 C 级，保持 C"
                )
            else:
                diagnostics.append(
                    f"{tag}：第 6 道核验：模型证据与候选片段重叠不足"
                    f"（{ratio:.2f} < {OVERLAP_MIN}），{grade} 级降为 {lowered} 级"
                )
                grade = lowered
    else:
        paragraph = _paragraph_of(contract_text, start)
        span = contract_text[start : min(end, start + MAX_SPAN)]

    reason = str(finding_raw.get("reason", "") or "").strip()[:MAX_REASON]
    if not reason:
        diagnostics.append(f"{tag}：缺理由，按已引法条给出通用描述")
        reason = "该约定与所引法条存在冲突，具体冲突点模型未给出"

    raw_facts = finding_raw.get("need_facts") or []
    facts = tuple(
        str(x).strip()[:MAX_FACT_CHARS]
        for x in (raw_facts if isinstance(raw_facts, list) else [])
        if str(x).strip()
    )[:MAX_FACTS]

    return ClauseFinding(
        paragraph=paragraph,
        span=span,
        grade=grade,
        reason=reason,
        statute_ids=ids,
        trigger_id=candidate.trigger_id if candidate is not None else "",
        source="trigger" if candidate is not None else "model_extra",
        need_facts=facts,
        quote_statute=quote,
    )


def _collect(
    raw: dict[str, Any],
    batch: Sequence[ClauseCandidate],
    contract_text: str,
    library: Mapping[str, StatuteEntry],
    diagnostics: list[str],
    seen: set[tuple[int, str]],
    batch_no: int,
    *,
    verify: bool = True,
) -> tuple[list[ClauseFinding], list[dict[str, Any]]]:
    """收一批模型输出：候选结论当场核验，自提结论只收集原始行（全局封顶在调用方做）。"""
    found: list[ClauseFinding] = []
    rows = raw.get("findings")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                diagnostics.append(f"第 {batch_no} 批：有 1 条结论不是对象，已丢弃")
                continue
            idx = row.get("idx")
            # bool 是 int 的子类：JSON 里的 true 会通过 isinstance(idx, int)，被当成 idx=1 命中候选，
            # 必须显式排除，否则模型给错类型时会静默落到别的候选上。
            if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < len(batch):
                diagnostics.append(f"第 {batch_no} 批：idx 越界（{idx}），已丢弃")
                continue
            finding = _verify(row, batch[idx], contract_text, library, errors=diagnostics, verify=verify)
            if finding is None:
                continue
            key = (finding.paragraph, finding.trigger_id)
            if key in seen:
                diagnostics.append(f"{finding.trigger_id}：同一段落重复结论，只保留第一条")
                continue
            seen.add(key)
            found.append(finding)
    elif rows is not None:
        diagnostics.append(f"第 {batch_no} 批：findings 不是列表，已忽略")
    else:
        diagnostics.append(f"第 {batch_no} 批：输出缺少 findings 字段，已忽略")

    extras = raw.get("extra")
    extra_rows = [row for row in extras if isinstance(row, dict)] if isinstance(extras, list) else []
    if isinstance(extras, list) and len(extra_rows) != len(extras):
        diagnostics.append(f"第 {batch_no} 批：有 {len(extras) - len(extra_rows)} 条自提结论不是对象，已丢弃")
    return found, extra_rows


def review_clauses(
    llm: Generator | None,
    candidates: Sequence[ClauseCandidate],
    contract_text: str,
    *,
    batch_size: int = 5,
    max_extra: int = 3,
    errors: list[str] | None = None,
    statutes: Mapping[str, StatuteEntry] | None = None,
    verify: bool = True,
    rule_checks: Sequence[RuleCheck] | None = None,
    facts: Mapping[str, Any] | None = None,
) -> tuple[ClauseFinding, ...]:
    """批量判定候选条款并做程序核验，返回按（段落、等级、触发器）稳定排序的结论。

    `errors` 是可选的外置诊断列表（调用方据此判断是否"条款审查未启用"）；
    `statutes` 可注入法源库（单测用假库，不读文件）。
    `rule_checks` / `facts` 是随 payload 下发对照的结构化数据（规格 003 第 6.1 节 / 第 13 章 P2-3）：
    校验信号只挑 mismatch / needs_fact，且只发与本批候选 `candidate_statute_ids` 相关的规则；
    事实只发已填的字段 id，并优先保住参与校验的字段。两者**只作判断参考**，
    不参与核验、不改变任何结论——模型是否采信由它自己决定（规则不是判官）。
    自提结论（`extra`）按**整份合同**封顶 `max_extra` 条，且与已命中条款重叠的不重复报
    （同一句里的多个问题由触发器按事由分别召回，不靠自提拆分，见 `_is_duplicate`）。
    `verify=False` 是**消融对照臂**（`model_raw`）：等级与法条 ID 原样保留，**不做自提封顶 C**
    （自提条目也照模型给的等级输出，生产臂一律封顶 C）；诊断里只剩"自提结论重叠去重"
    这类提示，不再出现核验丢弃；条数封顶与去重仍保留（它们只是输出规模约束，不是核验）。
    """
    diagnostics = errors if errors is not None else []
    if not candidates:
        return ()
    if llm is None or not bool(getattr(llm, "available", True)):
        diagnostics.append("条款审查未启用：未配置大模型")
        return ()

    library: Mapping[str, StatuteEntry] = statutes if statutes is not None else _statute_library()
    size = max(1, int(batch_size))
    batches: Iterable[Sequence[ClauseCandidate]] = (
        candidates[i : i + size] for i in range(0, len(candidates), size)
    )
    system = REVIEW_SYSTEM.replace("__MAX_EXTRA__", str(max(0, int(max_extra))))
    seen: set[tuple[int, str]] = set()
    out: list[ClauseFinding] = []
    extra_rows: list[dict[str, Any]] = []
    for batch_no, batch in enumerate(batches, start=1):
        # rule_checks / fact_sheet 按**本批候选**算（并集 / 字段优先级都随批变化），不能预先算一次。
        payload = json.dumps(
            _payload(
                batch,
                contract_text,
                library,
                max_extra,
                rule_checks=rule_checks,
                facts=facts,
                diagnostics=diagnostics,
            ),
            ensure_ascii=False,
        )
        try:
            raw = llm.generate(system, payload, {"type": "object"})
        except Exception as exc:  # noqa: BLE001 - 模型失败统一降级，不阻塞主流程
            diagnostics.append(f"第 {batch_no} 批条款判定调用失败：{exc}")
            continue
        if not isinstance(raw, dict):
            diagnostics.append(f"第 {batch_no} 批：模型输出不是 JSON 对象，已忽略")
            continue
        found, rows = _collect(raw, batch, contract_text, library, diagnostics, seen, batch_no, verify=verify)
        out.extend(found)
        extra_rows.extend(rows)

    # 自提结论：全局封顶 max_extra、一律降 C、与已命中条款重叠的不重复报（那不是"触发器之外"的新发现）
    # 封顶只限制**新增**条数：已达上限时继续遍历、只计数并记一条汇总诊断（
    # 不能直接 break，否则余下条目连诊断都没有，统计模型自提总量时会少计）；
    # 已收集的结论永不回退/清空。
    limit = max(0, int(max_extra))
    kept = 0
    duplicated = 0
    overflow = 0
    for row in extra_rows:
        if kept >= limit:
            overflow += 1
            continue
        finding = _verify(row, None, contract_text, library, errors=diagnostics, verify=verify)
        if finding is None:
            continue
        if _is_duplicate(finding, out):
            duplicated += 1
            continue
        out.append(finding)
        kept += 1
    if duplicated:
        diagnostics.append(f"自提结论 {duplicated} 条与已命中条款重叠，已丢弃（不重复报同一处约定）")
    if overflow:
        diagnostics.append(f"自提结论另有 {overflow} 条因封顶（{limit} 条）未处理（配额已满，未核验）")

    out.sort(key=lambda f: (f.paragraph, _grade_rank(f.grade), f.trigger_id, f.source, f.span))
    return tuple(out)
