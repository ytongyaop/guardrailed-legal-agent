"""模型辅助抽取（LLM 档）：正则先跑，模型只补缺失，逐字段校验后合并。

红线：
- 模型只补字段，不给结论；正则已有值一律优先（冲突只记诊断，不覆盖）；
- 任何调用失败都回退正则并标记降级，禁止用模板值或猜测值补字段；
- prompt 只留在实现层，内核（ports / graph）不出现 prompt 字符串。
"""

from __future__ import annotations

import re
from typing import Any

from ..config import load_config
from ..ports.extraction import ExtractionResult, Extractor
from ..ports.generation import Generator
from .extract import RegexExtractor, _norm
from .llm import LLMError

NO_KEY_ERROR = "本次未使用模型抽取：未配置 Key"
TIMEOUT_ERROR = "模型抽取超时，已回退规则式"
PARSE_ERROR = "模型抽取返回无法解析，已回退规则式"

EXTRACT_SYSTEM = """你是劳动合同关键条款抽取器。只输出 JSON，不要解释。
输出格式：{"facts": {"字段名": {"value": "归一化值", "evidence": "合同原文片段"}}}
要求：
1. 只抽取下面字段清单里的字段；合同没有明确写到的字段不要输出。
2. value 一律给纯数字字符串：不带单位、不带百分号；比例换算成百分数（“1.5 倍”写 "150"，
   “八成”写 "80"）；“半年”写 "6"；“每季度发放”写 "0.25"。
3. evidence 必须是合同正文中连续出现的一段原文（1-60 字），不得改写、拼接或概括。
4. 枚举字段只能输出清单给出的字面量，不得用同义词（“有限公司”不是合法枚举值）。
5. 用户消息里“已抽取字段”列出的字段不要重复输出。

字段清单：
- probation_months：试用期月数（0-24）
- probation_times：试用期约定次数（1-10）
- probation_wage_ratio：试用期工资比例（百分数，1-120）
- months_without_contract：未签书面合同月数（0-60）
- noncompete_years：竞业限制年限（年，0-10）
- noncompete_unpaid_months：未付竞业补偿月数（0-60）
- ot_hours_day：每日加班小时（0-24）
- ot_hours_month：每月加班小时（0-300）
- ot_weekday_ratio：工作日加班工资比例（百分数，50-500）
- ot_restday_ratio：休息日加班工资比例（百分数，50-500）
- ot_holiday_ratio：法定节假日加班工资比例（百分数，50-500）
- days_to_si_register：办理社保登记天数（0-365）
- si_fine_multiple：欠缴社保罚款倍数（0-10）
- annual_leave_days：年休假天数（0-30）
- unused_leave_pay_ratio：未休年假补偿比例（百分数，50-500）
- pay_times_per_month：每月工资发放次数（0-31）
- term_years：合同期限年数（0-40，0 表示无固定期限）
- term_type：合同期限类型，只能是 固定期限 / 无固定期限
- work_years：工作年限（0-50）
- employer_type：用工主体类型，只能是 有限责任公司 / 个体工商户 / 个人独资企业
- written_contract：是否签订书面劳动合同，只能是 是 / 否
- social_insurance：是否缴纳社会保险，只能是 是 / 否
"""

_MONTH_FIELDS = frozenset({"probation_months", "months_without_contract", "noncompete_unpaid_months"})
_YEAR_FIELDS = frozenset({"noncompete_years", "term_years", "work_years"})
_PERCENT_FIELDS = frozenset(
    {
        "probation_wage_ratio",
        "ot_weekday_ratio",
        "ot_restday_ratio",
        "ot_holiday_ratio",
        "unused_leave_pay_ratio",
    }
)

_NUMERIC_RANGES: dict[str, tuple[float, float]] = {
    "probation_months": (0, 24),
    "probation_times": (1, 10),
    "probation_wage_ratio": (1, 120),
    "months_without_contract": (0, 60),
    "noncompete_years": (0, 10),
    "noncompete_unpaid_months": (0, 60),
    "ot_hours_day": (0, 24),
    "ot_hours_month": (0, 300),
    "ot_weekday_ratio": (50, 500),
    "ot_restday_ratio": (50, 500),
    "ot_holiday_ratio": (50, 500),
    "days_to_si_register": (0, 365),
    "si_fine_multiple": (0, 10),
    "annual_leave_days": (0, 30),
    "unused_leave_pay_ratio": (50, 500),
    "pay_times_per_month": (0, 31),
    "term_years": (0, 40),
    "work_years": (0, 50),
}

_ENUM_VALUES: dict[str, tuple[str, ...]] = {
    "employer_type": ("有限责任公司", "个体工商户", "个人独资企业"),
    "written_contract": ("是", "否"),
    "social_insurance": ("是", "否"),
    "term_type": ("固定期限", "无固定期限"),
}

_FIELDS = frozenset(_NUMERIC_RANGES) | frozenset(_ENUM_VALUES)

_CN_DIGIT = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "壹": 1,
    "二": 2,
    "两": 2,
    "贰": 2,
    "三": 3,
    "叁": 3,
    "四": 4,
    "肆": 4,
    "五": 5,
    "伍": 5,
    "六": 6,
    "陆": 6,
    "七": 7,
    "柒": 7,
    "八": 8,
    "捌": 8,
    "九": 9,
    "玖": 9,
}
_CN_UNIT = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_CN_RUN = re.compile(r"[零〇一二两三四五六七八九十百千壹贰叁肆伍陆柒捌玖拾佰仟点]+")
_UNIT_SUFFIX = re.compile(r"(个月|月|年|小时|个?天|日|次|倍|成|%|％|以上|以下|以内|左右)$")
_PAY_TIMES_WORDS = (
    ("每季度", "0.25"),
    ("每季", "0.25"),
    ("每半年", "0.5"),  # 半年发一次等于每月 0.5 次；原先落进 `半年` 分支被丢弃成 None
    ("每两个月", "0.5"),
    ("每两月", "0.5"),
    ("每两周", "2"),  # 同一词表缺口：两周发一次 = 每月 2 次
    ("每半个月", "2"),
    ("每月", "1"),
)


def _cn_int(text: str) -> int | None:
    """中文数字 → 整数（支持 十二 / 二十四 / 百分之八十 去掉量词后的写法）。"""
    if not text:
        return None
    section = 0
    num = 0
    seen = False
    for ch in text:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
            seen = True
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            section += (num or 1) * unit
            num = 0
            seen = True
        else:
            return None
    return section + num if seen else None


def _cn_number(text: str) -> float | None:
    if not text:
        return None
    if "点" in text:
        left, _, right = text.partition("点")
        base = _cn_int(left)
        if base is None or not right or any(ch not in _CN_DIGIT for ch in right):
            return None
        frac = "".join(str(_CN_DIGIT[ch]) for ch in right)
        return float(f"{base}.{frac}")
    value = _cn_int(text)
    return None if value is None else float(value)


def _parse_number(token: str) -> float | None:
    """从值里取数字：优先阿拉伯数字，其次中文数字。"""
    text = _norm(token).replace("百分之", "")
    while True:
        stripped = _UNIT_SUFFIX.sub("", text)
        if stripped == text:
            break
        text = stripped
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match:
        return float(match.group())
    run = _CN_RUN.search(text)
    if run:
        return _cn_number(run.group())
    return None


def _fmt(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _normalize_value(field: str, raw: str) -> str | None:
    """按 2.3 表的归一化要求转成纯数字字符串；转不了返回 None。"""
    text = _norm(raw)
    if not text:
        return None
    if field == "pay_times_per_month":
        for word, number in _PAY_TIMES_WORDS:
            text = text.replace(word, number)
    if "半年" in text:
        if field in _MONTH_FIELDS:
            return "6"
        if field in _YEAR_FIELDS:
            return "0.5"
    if field in _PERCENT_FIELDS:
        if "成" in text:
            number = _parse_number(text.replace("成", ""))
            return None if number is None else _fmt(number * 10)
        if "倍" in text:
            number = _parse_number(text.replace("倍", ""))
            return None if number is None else _fmt(number * 100)
    number = _parse_number(text)
    return None if number is None else _fmt(number)


def _same_value(left: str, right: str) -> bool:
    try:
        return abs(float(left) - float(right)) < 1e-9
    except (TypeError, ValueError):
        return left == right


def _validate(field: str, row: Any, norm_text: str) -> tuple[str | None, str, str]:
    """五道校验：白名单 → 归一化 → 区间 → 证据 → 枚举。返回 (值, 证据, 丢弃原因)。"""
    if field not in _FIELDS:
        return None, "", "字段不在白名单"
    if not isinstance(row, dict):
        return None, "", "条目不是对象"
    raw_value = row.get("value")
    if raw_value is None or not str(raw_value).strip():
        return None, "", "缺少 value"

    if field in _ENUM_VALUES:
        value = _norm(str(raw_value))
        if value not in _ENUM_VALUES[field]:
            allowed = " / ".join(_ENUM_VALUES[field])
            return None, "", f"枚举值必须为 {allowed}（收到 {value}）"
    else:
        value = _normalize_value(field, str(raw_value))
        if value is None:
            return None, "", f"值无法归一化为数字（{raw_value}）"
        low, high = _NUMERIC_RANGES[field]
        if not (low <= float(value) <= high):
            return None, "", f"数值 {value} 超出允许区间 {_fmt(low)}-{_fmt(high)}"

    raw_evidence = row.get("evidence")
    evidence = str(raw_evidence).strip() if raw_evidence is not None else ""
    if not evidence:
        return None, "", "缺少 evidence 原文片段"
    if len(evidence) > 60:
        return None, "", "evidence 超过 60 字"
    if _norm(evidence) not in norm_text:
        return None, "", "evidence 不是合同原文片段（证据未命中）"
    return value, evidence, ""


def _user_prompt(text: str, known: dict[str, str]) -> str:
    given = "；".join(f"{key}={known[key]}" for key in sorted(known)) if known else "无"
    return f"已抽取字段（不要重复输出）：{given}\n\n合同正文：\n{text}"


class LLMExtractor:
    """模型辅助抽取：正则先跑，模型只补缺失字段，逐字段校验后合并。"""

    def __init__(self, generator: Generator | None, fallback: Extractor | None = None) -> None:
        self.generator = generator
        self.fallback: Extractor = fallback or RegexExtractor()
        self.max_chars = load_config().extraction.max_chars

    def extract(self, text: str) -> ExtractionResult:
        base = self.fallback.extract(text)
        regex_facts = dict(base.facts)
        if not (text or "").strip():
            return base

        generator = self.generator
        if generator is None or not bool(getattr(generator, "available", True)):
            return self._degrade(base, (NO_KEY_ERROR,))

        # 成本护栏：正则已覆盖全部字段时不再调用模型
        if _FIELDS <= set(regex_facts):
            return base

        errors: list[str] = []
        payload_text = text
        limit = self.max_chars
        if limit > 0 and len(text) > limit:
            payload_text = text[:limit]
            errors.append(f"合同超过 {limit} 字，模型抽取仅使用前 {limit} 字")

        try:
            raw = generator.generate(
                EXTRACT_SYSTEM, _user_prompt(payload_text, regex_facts), {"type": "object"}
            )
        except TimeoutError:
            return self._degrade(base, (*errors, TIMEOUT_ERROR))
        except LLMError as exc:
            return self._degrade(base, (*errors, f"模型抽取失败（{exc}），已回退规则式"))
        except Exception as exc:  # noqa: BLE001 - 任何调用异常都按降级处理，不阻塞核对
            return self._degrade(base, (*errors, f"模型抽取失败（{exc}），已回退规则式"))

        if not isinstance(raw, dict) or not isinstance(raw.get("facts"), dict):
            return self._degrade(base, (*errors, PARSE_ERROR))

        facts = dict(base.facts)
        evidence = dict(base.evidence)
        norm_text = _norm(payload_text)
        for key, row in raw["facts"].items():
            field = str(key)
            value, snippet, reason = _validate(field, row, norm_text)
            if reason:
                errors.append(f"丢弃字段 {field}：{reason}")
                continue
            assert value is not None
            if field in regex_facts:
                if not _same_value(regex_facts[field], value):
                    errors.append(f"抽取冲突：{field} 正则={regex_facts[field]} 模型={value}，已取正则")
                continue
            facts[field] = value
            evidence[field] = snippet
        return ExtractionResult(facts=facts, evidence=evidence, degraded=False, errors=tuple(errors))

    @staticmethod
    def _degrade(base: ExtractionResult, errors: tuple[str, ...]) -> ExtractionResult:
        """回退正则：字段完全用正则结果，只标记降级与诊断。"""
        return ExtractionResult(
            facts=dict(base.facts),
            evidence=dict(base.evidence),
            degraded=True,
            errors=errors,
        )
