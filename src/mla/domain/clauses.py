"""条款触发器层（存在性域）：合同正文 → 可疑条款候选。

本层只负责**召回**（宁多勿漏），不做任何违法/合规判定；判定由模型完成，
再由程序核验层（法规库闭集 + 证据可定位 + 引用在候选集内）把关。

约定：
- 一个触发器 = 一个可疑模式：`patterns`（Python `re` 语法）+ `candidate_statute_ids`（闭集法条 ID）。
- 命中判定在 `_norm()`（去掉全部空白，与 `src/mla/adapters/extract.py` 同口径）后的文本上做；
  但 `span` 一律取自**原文切片**，保证能被「原文子串」校验。
- 分段：按行切分，忽略空行，`paragraph` 从 1 开始按非空行计。
- `span`：整行 ≤ 80 字时取整行；否则以命中点为中心截取 80 字（仍是原文子串）。
- 同一段落同一 trigger 只出一条；输出按 `(paragraph, trigger_id)` 排序，保证逐位可复现。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

from ..adapters.extract import _norm

MAX_SPAN = 80
SEVERITY_HINTS = ("A", "B", "C")
_REQUIRED = ("trigger_id", "name", "patterns", "candidate_statute_ids")


@dataclass(frozen=True)
class Trigger:
    """一条触发器定义（数据来自 `data/clauses/triggers.yaml`）。"""

    trigger_id: str
    name: str
    patterns: tuple[str, ...]
    candidate_statute_ids: tuple[str, ...]
    severity_hint: str = "C"
    ask_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClauseCandidate:
    """一条可疑条款候选（只召回，不定级；`severity_hint` 仅作提示）。"""

    paragraph: int
    span: str
    trigger_id: str
    name: str
    candidate_statute_ids: tuple[str, ...]
    severity_hint: str


@cache
def _compile(pattern: str) -> re.Pattern[str]:
    """编译并缓存正则（同一 pattern 只编译一次，结果确定）。"""
    return re.compile(pattern)


def _as_str_tuple(raw: object, field: str, ctx: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
        raise ValueError(f"{ctx} 的 {field} 必须是字符串列表")
    return tuple(raw)


def load_triggers(path: str | Path) -> tuple[Trigger, ...]:
    """读取触发器表（YAML 列表）并做结构校验；格式错误直接抛 ValueError。"""
    rows = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    if not isinstance(rows, list):
        raise ValueError("触发器表顶层必须是列表")

    triggers: list[Trigger] = []
    seen: set[str] = set()
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {i} 条触发器不是对象")
        missing = [k for k in _REQUIRED if k not in row]
        if missing:
            raise ValueError(f"第 {i} 条触发器缺字段: {missing}")
        tid = str(row["trigger_id"])
        ctx = f"第 {i} 条触发器（{tid}）"
        if tid in seen:
            raise ValueError(f"{ctx}: trigger_id 重复")
        seen.add(tid)
        patterns = _as_str_tuple(row["patterns"], "patterns", ctx)
        if not patterns:
            raise ValueError(f"{ctx}: patterns 不能为空")
        for pat in patterns:
            try:
                _compile(pat)
            except re.error as exc:
                raise ValueError(f"{ctx}: pattern 无法编译（{pat}）：{exc}") from exc
        statutes = _as_str_tuple(row["candidate_statute_ids"], "candidate_statute_ids", ctx)
        if not statutes:
            raise ValueError(f"{ctx}: candidate_statute_ids 不能为空（闭集引用）")
        hint = str(row.get("severity_hint", "C"))
        if hint not in SEVERITY_HINTS:
            raise ValueError(f"{ctx}: severity_hint 必须是 {' / '.join(SEVERITY_HINTS)}")
        ask = row.get("ask_fields", [])
        triggers.append(
            Trigger(
                trigger_id=tid,
                name=str(row["name"]),
                patterns=patterns,
                candidate_statute_ids=statutes,
                severity_hint=hint,
                ask_fields=_as_str_tuple(ask, "ask_fields", ctx),
            )
        )
    return tuple(triggers)


def _paragraphs(text: str) -> list[str]:
    """按行切分，忽略空行（`paragraph` = 下标 + 1）。"""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _norm_with_index(raw: str) -> tuple[str, list[int]]:
    """归一化一行（复用 `_norm`，与抽取层同口径），同时记下每个字符在原文中的下标。

    下标用于把归一化文本上的命中位置回映射成**原文** span。
    """
    norm = _norm(raw)
    index = [i for i, ch in enumerate(raw) if not ch.isspace()]
    # 防御性分支，故意保留：`_norm` 的 `\s` 与 `isspace()` 空白口径一致，
    # 字符串输入构造不出长度不一致（空白行已由 `_paragraphs` 过滤）；保留是为了口径被改动时
    # 直接报错，而不是静默用错位下标切出错误的 span。
    if len(index) != len(norm):  # 理论上不会发生：口径不一致就报错，不静默切错位置
        raise ValueError("归一化文本与原文下标长度不一致")
    return norm, index


def _window(raw: str, start: int, end: int) -> str:
    """整行不长时取整行；过长时以命中点为中心截 80 字（均为原文子串）。"""
    if len(raw) <= MAX_SPAN:
        return raw
    center = (start + end) // 2
    begin = max(0, center - MAX_SPAN // 2)
    begin = min(begin, start)  # 不把命中起点切掉
    begin = max(begin, end - MAX_SPAN)  # 不把命中终点切掉
    begin = max(0, min(begin, len(raw) - MAX_SPAN))
    return raw[begin : begin + MAX_SPAN]


def _first_match(norm: str, trigger: Trigger) -> tuple[int, int] | None:
    """取该触发器所有 pattern 中**最靠左**的一次命中（并列取更长者），保证确定性。"""
    best: tuple[int, int] | None = None
    for pat in trigger.patterns:
        m = _compile(pat).search(norm)
        if m is None:
            continue
        span = (m.start(), m.end())
        if best is None or (span[0], -span[1]) < (best[0], -best[1]):
            best = span
    return best


def detect_clauses(text: str, triggers: tuple[Trigger, ...]) -> tuple[ClauseCandidate, ...]:
    """逐段落、逐触发器召回可疑条款候选；纯函数，不读写文件、不联网。"""
    candidates: list[ClauseCandidate] = []
    for paragraph, raw in enumerate(_paragraphs(text), start=1):
        norm, index = _norm_with_index(raw)
        # 防御性分支，故意保留：空行已被 `_paragraphs` 过滤，`norm` 不会为空；
        # 保留它使「只含空白字符的一行」不会产出候选（成本一行 continue，收益是不依赖上游实现细节）。
        if not norm:
            continue
        for trigger in triggers:
            hit = _first_match(norm, trigger)
            if hit is None:
                continue
            start = index[hit[0]]
            end = index[hit[1] - 1] + 1
            candidates.append(
                ClauseCandidate(
                    paragraph=paragraph,
                    span=_window(raw, start, end),
                    trigger_id=trigger.trigger_id,
                    name=trigger.name,
                    candidate_statute_ids=trigger.candidate_statute_ids,
                    severity_hint=trigger.severity_hint,
                )
            )
    candidates.sort(key=lambda c: (c.paragraph, c.trigger_id))
    return tuple(candidates)
