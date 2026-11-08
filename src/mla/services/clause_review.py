"""条款级模型判定（存在性域）：候选 → 批量判定 → 程序核验 → 分级结论。

流水线：

1. 候选由触发器层产出（`domain.clauses.detect_clauses`），本层**不做召回**；
2. 每 `batch_size`（默认 5）条候选一次模型调用，批量判定，避免 20 条串行把耗时拖到 100 秒以上；
3. `_verify` 逐条做程序核验：等级合法 → 证据在原文可定位 → 法条在库 → 触发器条目引用在候选闭集内；
   A/B 级缺法条的降为 C（不丢弃），任一硬性不过即丢弃并记诊断；
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
from collections.abc import Container, Iterable, Mapping, Sequence
from functools import lru_cache
from typing import Any

from ..adapters.extract import _norm
from ..adapters.rag import load_statutes
from ..config import load_config
from ..domain.clauses import MAX_SPAN, ClauseCandidate
from ..domain.models import ClauseFinding, StatuteEntry
from ..ports.generation import Generator

GRADES = ("A", "B", "C")
STATUTE_EXCERPT = 200  # 每条法条原文摘录上限（字符）
MAX_REASON = 200
MAX_FACTS = 5
MAX_FACT_CHARS = 80
MAX_LINE_CHARS = 160  # 单行送入 prompt 的字符上限（超长行截断，避免整段公文淹没候选）
OVERLAP_MIN = 0.5  # 模型证据与候选 span 的重叠下限（低于此值只记诊断，不丢弃）

REVIEW_SYSTEM = """你是劳动合同条款风险审查助手。只输出 JSON 对象，不要解释，不要 Markdown。

输入两部分：contract_paragraphs（候选条款及其邻近段落，按段落编号）与 candidates（本批待判定的可疑条款）。
对每条候选判断：该约定与它给出的候选法条是否存在冲突，并按风险强度定级。

定级口径：
- A：候选法条明文禁止该约定，不需要补充事实即可判断；
- B：候选法条有相关规定，但需要补充事实（工资基数、岗位性质、审批备案情况等）或存在裁量空间；
- C：触发器命中但证据不足或语义不明确，无法定级。

硬性要求：
1. 每条候选只能从它自己的 candidate_statute_ids 里选法条，不得引用未列出的条号，不得编造条号。
2. reason 只写冲突点，句式写"该约定与 X 条某某规定不符/存在冲突"；不要写"该条款无效""该条款违法"这类终局结论。
3. evidence 必须从合同原文逐字连续抄写（10 到 80 字），不得改写、不得拼接、不得用省略号。
4. A、B 级必须给法条 ID：只写 1 条最直接的依据（把最主要的那条放第一位）；
   确有两条并列且缺一不可时才写第二条，最多 2 条；
   不要罗列"可能相关"的条文，也不要为了兜底加引"合同无效"这类总则条款。
5. need_facts 填"还要补什么事实才能定论"，A 级可留空。
6. 候选之外若另有发现，放进 extra，最多 __MAX_EXTRA__ 条，grade 一律写 C。
7. 没有把握就写 C，不要猜。

输出格式：
{"findings":[{"idx":0,"grade":"A","reason":"...","statute_ids":["LCL-35"],"evidence":"...","need_facts":[]}],"extra":[{"grade":"C","reason":"...","evidence":"...","statute_ids":[],"need_facts":[]}]}
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


def _statutes_payload(
    candidate: ClauseCandidate, library: Mapping[str, StatuteEntry]
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for sid in candidate.candidate_statute_ids:
        entry = library.get(sid)
        if entry is None:  # 触发器表已校验，这里只做防御：库缺条目时不塞空壳
            continue
        out.append(
            {
                "statute_id": sid,
                "law": entry.law_name,
                "article": entry.article_no,
                "text": (entry.text or "")[:STATUTE_EXCERPT],
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


def _payload(
    batch: Sequence[ClauseCandidate],
    contract_text: str,
    library: Mapping[str, StatuteEntry],
    max_extra: int,
) -> dict[str, Any]:
    focus = {c.paragraph for c in batch}
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
    )


def _verify(
    finding_raw: dict[str, Any],
    candidate: ClauseCandidate | None,
    contract_text: str,
    library_ids: Container[str],
    *,
    errors: list[str] | None = None,
    verify: bool = True,
) -> ClauseFinding | None:
    """单条程序核验：任一硬性不过返回 None（丢弃）；软性问题降级或只记诊断。

    硬性（丢弃）：等级不是 A/B/C；证据无法在原文定位；引用法条不在库；触发器条目引用越出候选闭集。
    软性（保留）：A/B 级缺法条 → 降为 C；模型证据与候选片段重叠不足 → span 仍取候选片段。
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
    located = _locate(evidence, contract_text, candidate.paragraph if candidate is not None else None)
    if located is None:
        diagnostics.append(f"{tag}：证据无法在合同原文定位，已丢弃")
        return None

    raw_ids = finding_raw.get("statute_ids") or []
    if not isinstance(raw_ids, list):
        diagnostics.append(f"{tag}：statute_ids 不是列表，已丢弃")
        return None
    ids = tuple(dict.fromkeys(str(x).strip() for x in raw_ids if str(x).strip()))
    unknown = [sid for sid in ids if sid not in library_ids]
    if unknown:
        diagnostics.append(f"{tag}：引用了法源库中不存在的条号 {' / '.join(unknown)}，已丢弃")
        return None
    if candidate is not None:
        allowed = set(candidate.candidate_statute_ids)
        outside = [sid for sid in ids if sid not in allowed]
        if outside:
            diagnostics.append(f"{tag}：引用越出候选闭集 {' / '.join(outside)}，已丢弃")
            return None

    grade = raw_grade if candidate is not None else "C"  # 模型自提一律封顶 C，不采信其等级
    if grade in ("A", "B") and not ids:
        diagnostics.append(f"{tag}：{grade} 级没有法条依据，已降为 C")
        grade = "C"

    start, end = located
    if candidate is not None:
        paragraph = candidate.paragraph
        span = candidate.span
        if _overlap_ratio(evidence, span) < OVERLAP_MIN:
            diagnostics.append(f"{tag}：模型证据与候选片段重叠不足，span 仍取候选片段")
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
    )


def _collect(
    raw: dict[str, Any],
    batch: Sequence[ClauseCandidate],
    contract_text: str,
    library_ids: Container[str],
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
            finding = _verify(row, batch[idx], contract_text, library_ids, errors=diagnostics, verify=verify)
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
) -> tuple[ClauseFinding, ...]:
    """批量判定候选条款并做程序核验，返回按（段落、等级、触发器）稳定排序的结论。

    `errors` 是可选的外置诊断列表（调用方据此判断是否"条款审查未启用"）；
    `statutes` 可注入法源库（单测用假库，不读文件）。
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
    library_ids = set(library)
    size = max(1, int(batch_size))
    batches: Iterable[Sequence[ClauseCandidate]] = (
        candidates[i : i + size] for i in range(0, len(candidates), size)
    )
    system = REVIEW_SYSTEM.replace("__MAX_EXTRA__", str(max(0, int(max_extra))))
    seen: set[tuple[int, str]] = set()
    out: list[ClauseFinding] = []
    extra_rows: list[dict[str, Any]] = []
    for batch_no, batch in enumerate(batches, start=1):
        payload = json.dumps(_payload(batch, contract_text, library, max_extra), ensure_ascii=False)
        try:
            raw = llm.generate(system, payload, {"type": "object"})
        except Exception as exc:  # noqa: BLE001 - 模型失败统一降级，不阻塞主流程
            diagnostics.append(f"第 {batch_no} 批条款判定调用失败：{exc}")
            continue
        if not isinstance(raw, dict):
            diagnostics.append(f"第 {batch_no} 批：模型输出不是 JSON 对象，已忽略")
            continue
        found, rows = _collect(
            raw, batch, contract_text, library_ids, diagnostics, seen, batch_no, verify=verify
        )
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
        finding = _verify(row, None, contract_text, library_ids, errors=diagnostics, verify=verify)
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
