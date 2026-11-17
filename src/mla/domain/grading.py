"""分级重构：把内部 A/B/C 等级翻译成用户可读的两层（规格 003 第 3.4 节 / 阶段 P2-5）。

对外只有两个维度：

- **影响度**（主）：`高` / `中` / `低`，决定分组与排序。唯一权威是候选段落归类到的合同要素
  （`elements.yaml` 的 `impact`），**不采信模型自由判断**；模型可给 `impact_suggest` 作建议，
  与要素值不一致时以要素值为准并记诊断。
- **确定性**（次）：`确定冲突` / `待确认`，由内部 `grade` 与条目来源机械映射：
  `A`（法条明文 + 三件套齐全）与规则数值校验条目（`source="rule_check"`，程序算出的确定
  不一致）→ `确定冲突`；`B`（需补充事实 / 含裁量）、`C`、未知等级 → `待确认`。

内部 A/B/C 继续服务六道核验与降级链，**不再对外展示**。本模块是纯函数：不读环境、不写库、
不做分支决策；要素骨架可注入（测试给固定骨架），缺省时读包内文件一次（`default_elements` 已缓存）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .elements import IMPACTS, Element, default_elements

IMPACT_HIGH = "高"
IMPACT_MID = "中"
IMPACT_LOW = "低"
# 排序序号：越小越靠前（规格 3.4：高 > 中 > 低）
IMPACT_RANK: dict[str, int] = {IMPACT_HIGH: 0, IMPACT_MID: 1, IMPACT_LOW: 2}

CERTAINTY_CONFLICT = "确定冲突"
CERTAINTY_PENDING = "待确认"
# 排序序号：同影响度内先列确定冲突，再列待确认
CERTAINTY_RANK: dict[str, int] = {CERTAINTY_CONFLICT: 0, CERTAINTY_PENDING: 1}

# 确定性只有"确定冲突"这一档的等级：A（法条明文禁止 + 法条编号、合同原文、法条摘抄三件套齐全）。
# B 是"需补充事实或含裁量"，与 C 同属"待确认"——把 B 放进冲突层等于把待核实说成确定结论。
CONFLICT_GRADES = ("A",)

# 程序追加的数值校验条目来源：等级虽然是 C（模型没提这条），但它是规则算出来的确定不一致，
# 对外必须与 A 级同列"确定冲突"。与 `services.consensus.SOURCE_RULE_CHECK` 同一取值
# （domain 不依赖 services，故在此重写一次字面量）。
RULE_CHECK_SOURCE = "rule_check"

# 来源排序序号：同影响度、同确定性内，"有段落定位的法条明文冲突"先于"程序数值校验"。
# 后者没有合同段落，段落键恒为 0；不加这一键它会插到有段落的 A 级前面。
# 只影响"确定冲突"层内部：待确认条目一律取默认值 0，排序仍按段落号，行为不变。
SOURCE_RANK_DEFAULT = 0
SOURCE_RANK_RULE_CHECK = 1

# 未知要素的影响度：按"中"。
#
# 归不到任何要素的段落不是"形式瑕疵"，而是分类器没认出它——把这类真实条款压到列表底部，
# 等于让最不确定的东西最不显眼。按"中"处理是**防降级守卫**：宁可让用户多看一眼，
# 也不让分类失败变成降级理由。空 element_id（模型自提 / 旧记录）与库内不存在的 ID 同口径。
# 兜底要素 EL-13 的数据本身就是"中"（`elements.yaml`），这里不再对它单独硬编码：
# 影响度只有一个来源（要素数据），运行时覆盖会制造第二份真相。
FALLBACK_IMPACT = IMPACT_MID


@dataclass(frozen=True)
class DisplayLevel:
    """一条结论的对外展示层级：影响度 + 确定性 + 全序排序键。

    排序键四键全序：影响度（高→中→低）、确定性（确定冲突→待确认）、来源
    （法条明文冲突→程序数值校验）、合同段落号。
    """

    impact: str
    certainty: str
    sort_key: tuple[int, int, int, int]


def _text(value: object) -> str:
    return str(value or "").strip()


def paragraph_of(clause_ref: object) -> int:
    """`clause_ref` → 段落号；非纯数字（旧路径写的是"第十九条"、程序复核条目为空）取 0。"""
    raw = _text(clause_ref)
    return int(raw) if raw.isdigit() else 0


def source_rank(source: object) -> int:
    """来源优先级：规则数值校验条目（无段落定位）排在有段落的 A 级之后；其余来源同一档。"""
    return SOURCE_RANK_RULE_CHECK if _text(source) == RULE_CHECK_SOURCE else SOURCE_RANK_DEFAULT


def certainty_for(grade: object, source: str = "") -> str:
    """内部等级 + 来源 → 对外确定性：确定冲突 / 待确认（宁可低估）。

    - 规则数值校验条目（`source=rule_check`）：内部等级是 C，但它是程序按法条数值算出的
      确定不一致，对外必须与 A 级同列"确定冲突"；
    - A：法条明文禁止且三件套齐全 → 确定冲突；
    - B（需补充事实或含裁量）/ C / 未知等级 → 待确认。
    """
    if _text(source) == RULE_CHECK_SOURCE:
        return CERTAINTY_CONFLICT
    return CERTAINTY_CONFLICT if _text(grade).upper() in CONFLICT_GRADES else CERTAINTY_PENDING


def impact_for(element_id: object, elements: Sequence[Element] | None = None) -> str:
    """要素 ID → 影响度；未知/空 ID 一律"中"（见 `FALLBACK_IMPACT` 的原因）。

    兜底要素 EL-13 的数据已是"中"（4.3 口径：兜底不暗示"不重要"），因此走正常查表；
    只有库里查不到的 ID 才落 `FALLBACK_IMPACT`。
    """
    eid = _text(element_id)
    library = default_elements() if elements is None else tuple(elements)
    for element in library:
        if element.element_id == eid:
            return element.impact
    return FALLBACK_IMPACT


def _note(diagnostics: list[str] | None, message: str) -> None:
    """诊断去重（同一段落可能被多次求值），保证同一输入得到同一份诊断列表。"""
    if diagnostics is not None and message not in diagnostics:
        diagnostics.append(message)


def to_display(
    grade: str,
    element_id: str,
    paragraph: int,
    elements: Sequence[Element] | None = None,
    *,
    impact_suggest: str = "",
    diagnostics: list[str] | None = None,
    source: str = "",
) -> DisplayLevel:
    """单条结论 → `DisplayLevel`。

    影响度只认要素值；模型建议（`impact_suggest`）越域或与要素不一致时**不改变**结果，
    只往 `diagnostics` 追加一句可审计的说明（规格 3.4 / 6.2）。

    `source` 参与确定性判定与排序：规则数值校验条目（`rule_check`）即便等级是 C，也
    算"确定冲突"，且同层内排在有段落的 A 级之后。
    """
    impact = impact_for(element_id, elements)
    suggest = _text(impact_suggest)
    if suggest:
        if suggest not in IMPACTS:
            _note(
                diagnostics,
                f"第 {int(paragraph or 0)} 段：模型建议影响度「{suggest}」不在 高/中/低 内，已忽略",
            )
        elif suggest != impact:
            _note(
                diagnostics,
                f"第 {int(paragraph or 0)} 段：模型建议影响度「{suggest}」与要素"
                f"「{_text(element_id)}」的「{impact}」不一致，按要素值处理",
            )
    certainty = certainty_for(grade, source)
    sort_key = (
        IMPACT_RANK[impact],
        CERTAINTY_RANK[certainty],
        source_rank(source),
        int(paragraph or 0),
    )
    return DisplayLevel(impact=impact, certainty=certainty, sort_key=sort_key)


def display_of(item: object, elements: Sequence[Element] | None = None) -> DisplayLevel:
    """`CheckItem` 形态的条目 → `DisplayLevel`。

    条目上已填好 `impact` / `certainty`（共识层产出时填）就直接用，避免展示层二次求值；
    旧记录 / 旧路径没这两项时按 `grade` + `source` 现算（`element_id` 为空则落"中"，与兜底同口径）。

    规则数值校验条目例外：历史记录里可能存着旧口径（`待确认`，甚至更早的旧层名），
    与"程序算出的确定不一致"矛盾，一律按来源重算，不信落库值。
    """
    impact = _text(getattr(item, "impact", ""))
    certainty = _text(getattr(item, "certainty", ""))
    grade = _text(getattr(item, "grade", ""))
    source = _text(getattr(item, "source", ""))
    paragraph = paragraph_of(getattr(item, "clause_ref", ""))
    if source == RULE_CHECK_SOURCE and grade.upper() not in CONFLICT_GRADES:
        certainty = ""  # 旧口径的存值不可信，走重算
    if impact in IMPACT_RANK and certainty in CERTAINTY_RANK:
        return DisplayLevel(
            impact=impact,
            certainty=certainty,
            sort_key=(IMPACT_RANK[impact], CERTAINTY_RANK[certainty], source_rank(source), paragraph),
        )
    return to_display(
        grade,
        _text(getattr(item, "element_id", "")),
        paragraph,
        elements,
        source=source,
    )


def sort_items(items: Sequence[object], elements: Sequence[Element] | None = None) -> list:
    """按"影响度 → 确定性 → 来源 → 段落号"稳定排序；不改入参，同输入必然同输出。"""
    return sorted(list(items), key=lambda item: display_of(item, elements).sort_key)
