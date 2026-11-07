"""条款级 gold 集校验器。

检查项：
1. JSONL 每行可解析，八字段齐全（gold_id / contract / clause / span /
   grade / statute_ids / need_facts / note）
2. grade ∈ {A, B, C, 合规}；gold_id 不重复
3. `contract` 指向的文件存在
4. `span` 经 `_norm()` 归一化后是合同全文（同样归一化）的子串，且出现次数 ≥1
   —— 出现次数 >1 只告警（定位不唯一，模型核对时会取错位置）
5. `statute_ids` 全部存在于法源库
6. 正样本（grade ≠ 合规）必须有非空 `statute_ids`，除非 note 里写明了「待补库」
7. **全合规对照合同不得出现正样本**（`COMPLIANT_CONTRACTS` 里列出的合同，
   出现任何 grade ≠ 合规 的行即 FAIL —— 防止把对照样本误标成陷阱）

统计输出：按 `contract` 分组（每份合同正/负条数与 A/B/C 分布）、全局合计、
涉及法条去重数。

用法：
    python scripts/validate_clause_gold.py [data/evalsets/clause_gold.jsonl]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mla.adapters.extract import _norm

REQUIRED_FIELDS = (
    "gold_id",
    "contract",
    "clause",
    "span",
    "grade",
    "statute_ids",
    "need_facts",
    "note",
)
GRADES = ("A", "B", "C", "合规")
# 全合规对照样本：不得标注任何正样本（用于测 A 级误报，误标即失去对照意义）
COMPLIANT_CONTRACTS = ("contract_compliant_01.txt",)


def load_statutes(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[str(row["statute_id"])] = row
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("gold", nargs="?", default="data/evalsets/clause_gold.jsonl")
    ap.add_argument("--statutes", default="data/statutes/statutes.jsonl")
    args = ap.parse_args(argv)

    gold_path = Path(args.gold)
    if not gold_path.is_file():
        print(f"[FAIL] gold 文件不存在：{gold_path}")
        return 1

    statutes = load_statutes(Path(args.statutes))
    contract_cache: dict[str, str] = {}

    errors: list[str] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()
    positive = 0
    negative = 0
    grade_count = {"A": 0, "B": 0, "C": 0}
    cited: set[str] = set()
    # 每份合同的分组统计：行数、正/负条数、A/B/C 分布
    per_contract_stats: dict[str, dict[str, int]] = {}

    lines = gold_path.read_text(encoding="utf-8").splitlines()
    rows = 0
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        rows += 1
        tag = f"第 {lineno} 行"
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"{tag}: JSON 解析失败（{exc.msg}）")
            continue
        if not isinstance(row, dict):
            errors.append(f"{tag}: 不是 JSON 对象")
            continue

        missing = [f for f in REQUIRED_FIELDS if f not in row]
        if missing:
            errors.append(f"{tag}: 缺字段 {' / '.join(missing)}")
            continue

        gid = str(row["gold_id"])
        tag = f"{gid}（第 {lineno} 行）"
        if gid in seen_ids:
            errors.append(f"{tag}: gold_id 重复")
        seen_ids.add(gid)

        grade = str(row["grade"])
        if grade not in GRADES:
            errors.append(f"{tag}: grade「{grade}」不在 {' / '.join(GRADES)} 内")
            continue
        note = str(row["note"])
        # 统一按字符串列表处理，防止写成单个字符串时把 ID 拆成字符
        raw_ids = row["statute_ids"]
        if not isinstance(raw_ids, list) or any(not isinstance(x, str) for x in raw_ids):
            errors.append(f"{tag}: statute_ids 必须是字符串数组")
            raw_ids = []
        sids = [x for x in raw_ids if isinstance(x, str)]
        if not isinstance(row["need_facts"], list):
            errors.append(f"{tag}: need_facts 必须是数组")

        # 合同文件存在 + 片段可定位
        contract_name = str(row["contract"])
        contract_path = gold_path.parent / contract_name
        if contract_name not in contract_cache:
            if not contract_path.is_file():
                errors.append(f"{tag}: 合同文件不存在 {contract_path}")
                contract_cache[contract_name] = ""
            else:
                contract_cache[contract_name] = _norm(contract_path.read_text(encoding="utf-8"))
        contract_norm = contract_cache[contract_name]
        span_norm = _norm(str(row["span"]))
        if not span_norm:
            errors.append(f"{tag}: span 为空")
        elif contract_norm:
            hits = contract_norm.count(span_norm)
            if hits == 0:
                errors.append(f"{tag}: span 不是合同 {contract_name} 原文的子串（疑似改写）")
            elif hits > 1:
                warnings.append(f"{tag}: span 在合同中出现了 {hits} 次，定位不唯一")

        # 法条 ID 必须在库
        for sid in sids:
            if sid not in statutes:
                errors.append(f"{tag}: 法条不存在 {sid}")
            else:
                cited.add(sid)

        # 正样本引法要求
        stats = per_contract_stats.setdefault(
            contract_name, {"rows": 0, "positive": 0, "negative": 0, "A": 0, "B": 0, "C": 0}
        )
        stats["rows"] += 1
        if grade == "合规":
            negative += 1
            stats["negative"] += 1
        else:
            positive += 1
            grade_count[grade] += 1
            stats["positive"] += 1
            stats[grade] += 1
            if not sids and "待补库" not in note:
                errors.append(f"{tag}: 正样本（{grade} 级）没有 statute_ids，且 note 未注明「待补库」")
            # 全合规对照样本不得有正样本（防止把对照样本误标成陷阱）
            if contract_name in COMPLIANT_CONTRACTS:
                errors.append(
                    f"{tag}: 全合规对照合同 {contract_name} 出现正样本（{grade} 级），"
                    "对照样本失去 A 级误报分母的意义"
                )

    print(f"gold 文件：{gold_path}（{rows} 条）；法源库 {len(statutes)} 条")
    print("\n按合同分组：")
    for name in sorted(per_contract_stats):
        st = per_contract_stats[name]
        flag = "（全合规对照：正样本必须为 0）" if name in COMPLIANT_CONTRACTS else ""
        print(
            f"  · {name}：{st['rows']} 条 = 正 {st['positive']}"
            f"（A {st['A']} / B {st['B']} / C {st['C']}）+ 负 {st['negative']}{flag}"
        )
    if warnings:
        print(f"\n[WARN] {len(warnings)} 条定位不唯一：")
        for w in warnings:
            print("  -", w)
    if errors:
        print(f"\n[FAIL] {len(errors)} 个问题：")
        for e in errors:
            print("  -", e)
        return 1

    print(
        f"\n正样本 {positive} 条（A {grade_count['A']} / B {grade_count['B']} / C {grade_count['C']}）；"
        f"负样本 {negative} 条；涉及法条去重后 {len(cited)} 条"
    )
    print("\n[OK] 条款级 gold 全部通过校验")
    return 0


if __name__ == "__main__":
    sys.exit(main())
