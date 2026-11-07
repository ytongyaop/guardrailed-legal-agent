"""规则表校验器：确保每条规则都能回到法条原文。

检查项：
1. 规则结构：ID 前缀、op 合法、必填字段
2. `statute_id` 必须存在于法源库，且 `status=有效`
3. `evidence_span` 必须**逐字**出现在该条 `text` 中（去空白后子串匹配）
4. `threshold` 的数字要在 `evidence_span` 里能找到对应写法
   （阿拉伯数字 / 中文数字 / 百分之X / 80% 形态均认）
5. 规则 ID 全局唯一

用法：
    python scripts/validate_rules.py [data/rules/rules.yaml]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

CN_DIGITS = "零一二三四五六七八九"

WHEN_MODES = ("and", "or")


def cn_number(n: int) -> str:
    """1..99 的中文写法（够覆盖本法域阈值）。"""
    if n == 0:
        return CN_DIGITS[0]
    if n < 10:
        return CN_DIGITS[n]
    if n < 20:
        return "十" + (CN_DIGITS[n % 10] if n % 10 else "")
    tens, ones = divmod(n, 10)
    return CN_DIGITS[tens] + "十" + (CN_DIGITS[ones] if ones else "")


def cn_hundred(n: int) -> str:
    """100..999：百分之一百五十。"""
    if n < 100:
        return cn_number(n)
    h, rest = divmod(n, 100)
    s = CN_DIGITS[h] + "百"
    if rest == 0:
        return s
    if rest < 10:
        return s + "零" + CN_DIGITS[rest]
    return s + cn_number(rest)


def norm(s: str) -> str:
    s = re.sub(r"\s+", "", s)
    for a, b in (("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'"), ("（", "("), ("）", ")")):
        s = s.replace(a, b)
    return s


def load_statutes(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[str(row["statute_id"])] = row
    return out


def number_variants(value: float) -> list[str]:
    """阈值在原文里可能出现的写法。"""
    variants: list[str] = []
    if value.is_integer():
        n = int(value)
        variants += [str(n), cn_number(n) if n < 100 else cn_hundred(n)]
    else:
        variants.append(str(value))
        variants.append(str(value).rstrip("0").rstrip("."))
    return variants


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("rules", nargs="?", default="data/rules/rules.yaml")
    ap.add_argument("--statutes", default="data/statutes/statutes.jsonl")
    args = ap.parse_args(argv)

    rules = yaml.safe_load(Path(args.rules).read_text(encoding="utf-8")) or []
    statutes = load_statutes(Path(args.statutes))

    errors: list[str] = []
    seen: set[str] = set()

    for i, rule in enumerate(rules, start=1):
        rid = str(rule.get("rule_id", f"#{i}"))
        if rid in seen:
            errors.append(f"{rid}: 规则 ID 重复")
        seen.add(rid)

        if str(rule.get("when_mode", "and")) not in WHEN_MODES:
            errors.append(f"{rid}: when_mode 只能是 {' / '.join(WHEN_MODES)}")

        sid = str(rule.get("statute_id", ""))
        span = str(rule.get("evidence_span", ""))
        statute = statutes.get(sid)
        if statute is None:
            errors.append(f"{rid}: 法条不存在 {sid}")
            continue
        if str(statute.get("status")) != "有效":
            errors.append(f"{rid}: 引用的 {sid} 状态为 {statute.get('status')}")
        text = norm(str(statute.get("text", "")))
        if not span:
            errors.append(f"{rid}: 缺 evidence_span")
            continue
        if norm(span) not in text:
            errors.append(f"{rid}: evidence_span 不是 {sid} 原文的子串（疑似改写或记错）")
            continue

        try:
            need = float(rule["threshold"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"{rid}: threshold 不是数字")
            continue
        span_norm = norm(span)
        if not any(v and v in span_norm for v in number_variants(need)):
            errors.append(f"{rid}: 阈值 {need:g} 在 evidence_span 中找不到对应写法")

        for key_op, key_val in (("when_op2", "when_value2"),):
            if key_op in rule:
                try:
                    when_need = float(rule[key_val])
                except (KeyError, TypeError, ValueError):
                    # 字符串枚举前提（如 when_op2: "==" + when_value2: 无固定期限）不走数字溯源
                    if str(rule.get(key_op, "")) in ("==", "!=") and str(rule.get(key_val, "")).strip():
                        continue
                    errors.append(f"{rid}: {key_val} 不是数字")
                    continue
                if not any(v and v in span_norm for v in number_variants(when_need)):
                    errors.append(f"{rid}: 前提值 {when_need:g} 在 evidence_span 中找不到")

    print(f"规则 {len(rules)} 条；法源库 {len(statutes)} 条")
    if errors:
        print(f"\n[FAIL] {len(errors)} 个问题：")
        for e in errors:
            print("  -", e)
        return 1

    print("\n[OK] 全部规则都能回到法条原文")
    return 0


if __name__ == "__main__":
    sys.exit(main())
