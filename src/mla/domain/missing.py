"""合同必备条款缺项检测（纯规则、零模型）：只判"有没有痕迹"，不判内容合法性。

写了"自愿放弃社保"也算已写——违法性由触发器层负责，不在本层。
判定单位是**非空行**：每行单独匹配，不跨行拼接，避免标题行与正文行凑成一条"已写"。
`all` 模式下组是全文范围（不同组可命中不同行），`any` 模式是行内正则。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

from ..adapters.extract import _norm
from .models import MissingStatus, MissingTerm

JUDGES = ("presence", "not_judged")
MODES = ("any", "all")
_REQUIRED = ("term_id", "name", "clause_no", "statute_id", "evidence_span", "judge")
_HIGH = "高"


@dataclass(frozen=True)
class MissingTermSpec:
    """一条必备条款规格（数据来自 `src/mla/data/clauses/missing_terms.yaml`）。"""

    term_id: str
    name: str
    clause_no: str
    statute_id: str
    evidence_span: str
    judge: str
    certainty: str = "低"
    mode: str = "any"
    patterns: tuple[str, ...] = ()
    groups: tuple[tuple[str, ...], ...] = ()


def _str_list(raw: object, field: str, ctx: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
        raise ValueError(f"{ctx} 的 {field} 必须是字符串列表")
    return tuple(raw)


def _str_groups(raw: object, ctx: str) -> tuple[tuple[str, ...], ...]:
    if not isinstance(raw, list):
        raise ValueError(f"{ctx} 的 groups 必须是列表的列表")
    groups = tuple(_str_list(group, "groups 子组", ctx) for group in raw)
    if any(not group for group in groups):
        raise ValueError(f"{ctx}: groups 不能有空组（空组会永远判不命中）")
    return groups


@cache
def load_missing_terms(path: str | Path) -> tuple[MissingTermSpec, ...]:
    """读规格表并做结构校验；judge / mode 与 patterns / groups 的搭配错误直接抛 ValueError。"""
    rows = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    if not isinstance(rows, list):
        raise ValueError("规格表顶层必须是列表")

    specs: list[MissingTermSpec] = []
    seen: set[str] = set()
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {i} 条规格不是对象")
        absent = [k for k in _REQUIRED if k not in row]
        if absent:
            raise ValueError(f"第 {i} 条规格缺字段: {absent}")
        term_id = str(row["term_id"])
        ctx = f"第 {i} 条规格（{term_id}）"
        if term_id in seen:
            raise ValueError(f"{ctx}: term_id 重复")
        seen.add(term_id)
        judge = str(row["judge"])
        if judge not in JUDGES:
            raise ValueError(f"{ctx}: judge 必须是 {' / '.join(JUDGES)}")
        mode = str(row.get("mode", "any"))
        if mode not in MODES:
            raise ValueError(f"{ctx}: mode 必须是 {' / '.join(MODES)}")
        patterns = _str_list(row.get("patterns", []), "patterns", ctx)
        groups = _str_groups(row.get("groups", []), ctx)
        if judge == "presence":
            # 有且只有一种判据：any 用 patterns，all 用 groups
            if mode == "any" and (not patterns or groups):
                raise ValueError(f"{ctx}: mode=any 要求 patterns 非空且 groups 为空")
            if mode == "all" and (not groups or patterns):
                raise ValueError(f"{ctx}: mode=all 要求 groups 非空且 patterns 为空")
        elif patterns or groups:
            raise ValueError(f"{ctx}: judge=not_judged 不能带 patterns / groups")
        for pattern in patterns:
            try:
                _compile(pattern)
            except re.error as exc:
                raise ValueError(f"{ctx}: pattern 无法编译（{pattern}）：{exc}") from exc
        specs.append(
            MissingTermSpec(
                term_id=term_id,
                name=str(row["name"]),
                clause_no=str(row["clause_no"]),
                statute_id=str(row["statute_id"]),
                evidence_span=str(row["evidence_span"]),
                judge=judge,
                certainty=str(row.get("certainty", "低")),
                mode=mode,
                patterns=patterns,
                groups=groups,
            )
        )
    return tuple(specs)


@cache
def _compile(pattern: str) -> re.Pattern[str]:
    """编译并缓存正则（同一 pattern 只编译一次）。"""
    return re.compile(pattern)


def _lines(text: str) -> list[str]:
    """按非空行切分并归一化（去空白，与抽取层同口径）。"""
    return [_norm(line) for line in (text or "").splitlines() if line.strip()]


def _hit_any(lines: list[str], spec: MissingTermSpec) -> bool:
    """any 模式：任一 pattern 在**某一行**命中。"""
    return any(_compile(p).search(line) for line in lines for p in spec.patterns)


def _hit_all(lines: list[str], spec: MissingTermSpec) -> bool:
    """all 模式：每一组都有至少一个关键词出现在某一行（组跨行，不要求同行）。"""
    return all(any(kw in line for line in lines for kw in group) for group in spec.groups)


def _term_no(term_id: str) -> tuple[int, str]:
    """按编号排序（MT-2 排在 MT-10 前），编号缺失时退回字符串序。"""
    digits = "".join(ch for ch in term_id if ch.isdigit())
    return (int(digits) if digits else 0, term_id)


def _judge(spec: MissingTermSpec, lines: list[str], too_short: bool) -> tuple[MissingStatus, str]:
    if too_short:
        return MissingStatus.MANUAL, "正文不足，未判定"
    if spec.judge == "not_judged":
        return MissingStatus.MANUAL, "兜底事项，需人工确认"
    hit = _hit_any(lines, spec) if spec.mode == "any" else _hit_all(lines, spec)
    if hit:
        return MissingStatus.WRITTEN, ""
    if spec.certainty == _HIGH:
        return MissingStatus.ABSENT, "未检出必备条款表述"
    return MissingStatus.UNSEEN, "未检出，请人工确认"


def detect_missing_terms(
    text: str, specs: tuple[MissingTermSpec, ...], *, min_chars: int = 400
) -> tuple[MissingTerm, ...]:
    """逐类判定必备条款有没有写；输出按 term_id 排序，同样的输入必然同样的输出。

    正文短于 `min_chars` 时一律"需人工确认"——条文太短的合同不产生任何"缺失"断言。
    """
    lines = _lines(text)
    too_short = len(text or "") < min_chars
    out: list[MissingTerm] = []
    for spec in sorted(specs, key=lambda s: _term_no(s.term_id)):
        status, note = _judge(spec, lines, too_short)
        out.append(
            MissingTerm(
                term_id=spec.term_id,
                name=spec.name,
                clause_no=spec.clause_no,
                statute_id=spec.statute_id,
                status=status,
                certainty=spec.certainty,
                note=note,
            )
        )
    return tuple(out)
