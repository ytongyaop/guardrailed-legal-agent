"""L2 辅助层：合同要素骨架（数据加载 + 段落归类）。

规格 003 第 4.3 节：提示层给模型增加**注意力**，只写"合同由哪几部分组成、每部分该盯什么方向"，
不写数值、期限、百分比、法条条号，也不穷举"一句话有几种写法"（那些由 L1 引用与 L3 校验去管）。

数据在 `src/mla/data/clauses/elements.yaml`，与本模块分离：改提示只动数据，不动代码。

归类是**纯程序**：用 `section_hints`（段落特征词）对候选所在段落做包含匹配，
命中多个要素时依次比较"匹配词更长 → 出现更早 → 要素顺序"，全不命中落 `EL-13`（其他约定）。
选"匹配词更长"优先，是因为长词更具体（"保密"是泛词、"社会保险"是具体项），
按出现位置优先会把同时提到"岗位"与"工作制"的工时条款错归到工作内容要素。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from ..config import data_path

ELEMENT_IDS: tuple[str, ...] = tuple(f"EL-{i}" for i in range(1, 14))
IMPACTS = ("高", "中", "低")
FALLBACK_ELEMENT_ID = "EL-13"  # 兜底要素：前 12 项都没命中的段落（数据 impact=中，见 elements.yaml）
MIN_HINTS = 2
MAX_HINTS = 5
MIN_FOCUS = 3
MAX_FOCUS = 5
_REQUIRED = ("element_id", "name", "section_hints", "impact", "focus")


@dataclass(frozen=True)
class Element:
    """一个合同要素（数据来自 `src/mla/data/clauses/elements.yaml`）。

    `focus` 只写"要确认什么 / 要分清什么"，不写结论与阈值：L2 不进判定，
    写成断语等于把模型往预设答案推。
    """

    element_id: str
    name: str
    section_hints: tuple[str, ...]
    impact: str
    focus: tuple[str, ...]


def _as_str_tuple(raw: object, field: str, ctx: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
        raise ValueError(f"{ctx} 的 {field} 必须是字符串列表")
    return tuple(raw)


def load_elements(path: str | Path) -> tuple[Element, ...]:
    """读取要素骨架（YAML 列表）并做结构校验；格式错误直接抛 ValueError。

    只校验**结构**（字段齐备、类型、影响度取值、条数区间）；"有没有数值 / 条号 / 写法枚举"
    属内容约束，由 `scripts/validate_elements.py` 负责——两者分工与触发器表一致。
    """
    rows = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    if not isinstance(rows, list):
        raise ValueError("要素骨架顶层必须是列表")

    elements: list[Element] = []
    seen: set[str] = set()
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {i} 个要素不是对象")
        missing = [k for k in _REQUIRED if k not in row]
        if missing:
            raise ValueError(f"第 {i} 个要素缺字段: {missing}")
        eid = str(row["element_id"])
        ctx = f"第 {i} 个要素（{eid}）"
        if eid in seen:
            raise ValueError(f"{ctx}: element_id 重复")
        seen.add(eid)
        if eid not in ELEMENT_IDS:
            raise ValueError(f"{ctx}: element_id 必须是 {' / '.join(ELEMENT_IDS)}")
        name = str(row["name"]).strip()
        if not name:
            raise ValueError(f"{ctx}: name 不能为空")
        hints = _as_str_tuple(row["section_hints"], "section_hints", ctx)
        if not MIN_HINTS <= len(hints) <= MAX_HINTS:
            raise ValueError(f"{ctx}: section_hints 必须是 {MIN_HINTS}–{MAX_HINTS} 个")
        focus = _as_str_tuple(row["focus"], "focus", ctx)
        if not MIN_FOCUS <= len(focus) <= MAX_FOCUS:
            raise ValueError(f"{ctx}: focus 必须是 {MIN_FOCUS}–{MAX_FOCUS} 条")
        impact = str(row["impact"])
        if impact not in IMPACTS:
            raise ValueError(f"{ctx}: impact 必须是 {' / '.join(IMPACTS)}")
        elements.append(
            Element(
                element_id=eid,
                name=name,
                section_hints=hints,
                impact=impact,
                focus=focus,
            )
        )
    return tuple(elements)


@lru_cache(maxsize=1)
def default_elements() -> tuple[Element, ...]:
    """包内骨架（只读一次）。文件缺失 → 空骨架，注入按"文件不存在即不发"降级。"""
    try:
        return load_elements(data_path("clauses/elements.yaml"))
    except FileNotFoundError:
        return ()


def _score(text: str, elements: Sequence[Element]) -> tuple[tuple[int, int, int], Element] | None:
    """找出最贴合该段落的要素：匹配词更长 → 出现更早 → 要素顺序（保证确定性）。"""
    best: tuple[tuple[int, int, int], Element] | None = None
    for order, element in enumerate(elements):
        for hint in element.section_hints:
            pos = text.find(hint)
            if pos < 0:
                continue
            key = (-len(hint), pos, order)
            if best is None or key < best[0]:
                best = (key, element)
    return best


def classify_paragraph(text: str, elements: Sequence[Element]) -> tuple[Element | None, bool]:
    """段落归类：返回（要素, 是否真的命中了特征词）。

    `elements` 为空（骨架文件缺失）时返回 `(None, False)`，调用方据此不下发 `elements` 段；
    有骨架但一个特征词都没命中时落兜底要素 `EL-13`，`matched=False` 供调用方记诊断。
    """
    if not elements:
        return None, False
    best = _score(text or "", elements)
    if best is not None:
        return best[1], True
    for element in elements:
        if element.element_id == FALLBACK_ELEMENT_ID:
            return element, False
    # 防御性分支：骨架里没有 EL-13（validate_elements.py 会拦，这里只保证不崩）
    return None, False


def match_element_id(text: str, elements: Sequence[Element]) -> str:
    """段落 → 要素 ID；无骨架时返回空串（调用方据此不下发 `element` 字段）。"""
    element, _ = classify_paragraph(text, elements)
    return element.element_id if element is not None else ""


def element_payload(element: Element) -> dict[str, object]:
    """单个要素的 payload 形态：只发判断方向，不发 `section_hints`（那是归类用的，模型用不上）。"""
    return {
        "element_id": element.element_id,
        "name": element.name,
        "impact": element.impact,
        "focus": list(element.focus),
    }
