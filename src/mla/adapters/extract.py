"""合同关键项抽取（规则式为基线，可替换为模型抽取，接口不变）。

只抽"能判定规则"的字段，抽不到就**不写**（下游规则缺字段会跳过，不猜）。
"""

from __future__ import annotations

import re
from datetime import date

from ..ports.extraction import ExtractionResult

CN_NUM = {
    "零": 0,  # 与 LLM 档 `_CN_DIGIT` 口径统一：`cn_to_int("零")` 必须返回 0，不能当成缺值
    "两": 2,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    # 大写数字（合同里常见「柒个月」「捌」这类写法），与上面小写口径一致
    "柒": 7,
    "捌": 8,
    "玖": 9,
}


def cn_to_int(s: str) -> int | None:
    """把「一 / 十二 / 二十四 / 6 / 零」这类写法转成整数；空串与非法字符返回 None。"""
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        left, _, right = s.partition("十")
        tens = CN_NUM.get(left, 1) if left else 1
        ones = CN_NUM.get(right, 0) if right else 0
        return tens * 10 + ones
    total = 0
    for ch in s:
        if ch not in CN_NUM:
            return None
        total += CN_NUM[ch]
    # 不能写 `total or None`：那会把 `零` 的 0 当成缺值丢掉，与 LLM 档 `_cn_int("零") == 0`
    # 的口径不一致；直接返回累加值即可（空串已在上面返回 None，非法字符已返回 None，0 语义无歧义）。
    return total


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


# 社保否定式：否定词紧贴动词算一条，外加「放弃 / 折算为现金 / 现金替代」三类置换表述。
# 必须整体先于肯定式判定，否则「放弃缴纳社会保险」会被 (缴纳|办理).{0,4}(社保) 吞成「是」。
_SI_NEGATION = re.compile(
    r"(?:未|不|不再|不予|无需|免于|拒绝)"
    r"(?:为(?:乙方|员工|劳动者|其)?)?"
    r"(?:缴纳|缴|交|办理|申报).{0,4}(?:社保|社会保险)"
    r"|放弃(?:缴纳|缴|交|办理|参加)?.{0,8}(?:社保|社会保险)"
    r"|(?:社保|社会保险|保险费)(?:单位部分|单位缴费部分)?"
    r"(?:折算|折合|兑换|置换|换成)(?:为|成)?现金"
    r"|(?:现金|补贴).{0,8}(?:替代|代替|顶替).{0,6}(?:社保|社会保险)"
    # 社保转嫁的判定口径：劳动者自行缴纳 / 自行承担。必须带 `自行|自己` 这类强信号，
    # 否则「甲方依法为乙方缴纳社会保险」「乙方应当缴纳的部分由甲方代扣代缴」会被误判成「否」。
    r"|(?:乙方|员工|劳动者|职工|个人|本人)(?:应当|应|需|须|要)?(?:自行|自己)"
    r"(?:负责|缴纳|交纳|承担|负担|办理|支付|处理).{0,8}(?:社保|社会保险)"
    r"|(?:社保|社会保险费(?:用)?|社会保险|保险费)(?:的)?(?:单位部分|个人部分)?"
    r"(?:由)?(?:乙方|员工|劳动者|职工|个人|本人)(?:个人|自行|自己)"
    r"(?:承担|负担|缴纳|交纳|缴付|负责|自理|处理)"
    # 「社保…由个人…」：合法写法多是「由用人单位代扣代缴」，写成「由个人缴纳/承担」基本是转嫁。
    r"|(?:社保|社会保险费(?:用)?|社会保险|保险费).{0,4}?由个人"
    r"(?:缴纳|交纳|缴付|承担|负担|负责|自理|处理)"
    # 未参保 / 停止缴纳：只认 `未参加 / 未办理` + 社保，不认 `未办理参保手续`——
    # 后者常出现在迟延登记的违规描述里，用工事实仍是已参保，判成未参保会误报。
    r"|(?:未|没有|从未)(?:参加|办理).{0,4}(?:社保|社会保险)"
    r"|(?:不再|停止|中断|暂停)(?:为.{0,4})?(?:缴纳|交纳|缴付|办理).{0,4}(?:社保|社会保险)"
)


def extract_facts(text: str) -> dict[str, str]:
    """从合同文本抽取事实；抽不到的键不出现。"""
    t = _norm(text)
    facts: dict[str, str] = {}
    if not t:
        return facts

    # 「试用期：6 个月 / 试用期为六个月 / 试用期 6 个月 / 试用期约定为 6 个月」都要能抽到；
    # _norm 已去空白，这里只需容忍冒号、逗号与连接词。
    m = re.search(r"试用期(?:约定)?(?:为|是)?[：:，,]?([一二三四五六七八九十两柒捌玖\d]+)个?月", t)
    if m and (v := cn_to_int(m.group(1))) is not None:
        facts["probation_months"] = str(v)

    # 用工主体类型
    if "个体工商户" in t:
        facts["employer_type"] = "个体工商户"
    elif "个人独资" in t:
        facts["employer_type"] = "个人独资企业"
    elif "有限责任公司" in t or "有限公司" in t or "公司" in t:
        facts["employer_type"] = "有限责任公司"

    # 未签书面合同的月数
    m = re.search(r"(?:未|尚未|没有).{0,12}书面.{0,12}?(?:已)?满([一二三四五六七八九十两柒捌玖\d]+)个?月", t)
    if m and (v := cn_to_int(m.group(1))) is not None:
        facts["months_without_contract"] = str(v)

    m = re.search(r"合同期限(?:为|是)?([一二三四五六七八九十柒捌玖\d]+)年", t)
    if m and (v := cn_to_int(m.group(1))) is not None:
        facts["term_years"] = str(v)

    if "term_years" not in facts:
        m = re.search(r"自(\d{4})年(\d{1,2})月(\d{1,2})日[至到起].*?(\d{4})年(\d{1,2})月(\d{1,2})日", t)
        if m:
            try:
                start = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                end = date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
                facts["term_years"] = f"{(end - start).days / 365.25:.2f}"
            except ValueError:
                pass

    # 合同期限类型（"无固定期限"字面里也含"固定期限"，故先判它）
    if "无固定期限" in t:
        facts["term_type"] = "无固定期限"
    elif "固定期限" in t:
        facts["term_type"] = "固定期限"

    m = re.search(r"试用期(?:工资|报酬)(?:为|是|不低于)?[^%。；％]{0,12}?(\d{1,3})\s*[%％]", t)
    if m:
        facts["probation_wage_ratio"] = m.group(1)

    if re.search(r"(未|不)(与员工)?(签|订立).{0,6}书面", t) or "没有签订书面" in t:
        facts["written_contract"] = "否"
    elif "书面劳动合同" in t or "签订" in t:
        facts["written_contract"] = "是"

    # 社保：先判否定，再判肯定。否则「放弃缴纳社会保险」会被肯定式吞成「是」。
    if _SI_NEGATION.search(t):
        facts["social_insurance"] = "否"
    elif re.search(r"(缴纳|办理).{0,4}(社保|社会保险)", t):
        facts["social_insurance"] = "是"

    m = re.search(r"竞业限制(?:期限)?(?:为|是)?([一二三四五六七八九十柒捌玖\d]+)年", t)
    if m and (v := cn_to_int(m.group(1))) is not None:
        facts["noncompete_years"] = str(v)

    m = re.search(
        r"(?:每月)?加班(?:时间)?(?:不超过|不得超过|最多|上限为|为)?([一二三四五六七八九十两柒捌玖\d]+)小时", t
    )
    if m and (v := cn_to_int(m.group(1))) is not None:
        facts["ot_hours_month"] = str(v)

    return facts


class RegexExtractor:
    """纯正则抽取（确定性档）：不调用模型，字段结果与 `extract_facts` 完全一致。"""

    def extract(self, text: str) -> ExtractionResult:
        return ExtractionResult(facts=extract_facts(text))
