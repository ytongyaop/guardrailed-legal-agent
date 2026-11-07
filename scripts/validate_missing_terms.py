"""必备条款规格校验器：九条齐全、term_id 唯一、依据可溯源。

检查项：
1. 规格表结构（judge / mode 与 patterns / groups 搭配、正则可编译）由 `load_missing_terms` 把关；
2. `term_id` 恰为 MT-1..MT-9，顺序与唯一性都要对；
3. `statute_id` 必须存在于 `data/statutes/statutes.jsonl`；
4. `evidence_span` 必须是 `LCL-17` 条文原文的子串（依据片段不许自造）。

用法：`python scripts/validate_missing_terms.py`；任一硬校验不过，退出码 1。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mla.domain.missing import load_missing_terms

SPECS_PATH = Path("data/clauses/missing_terms.yaml")
STATUTES_PATH = Path("data/statutes/statutes.jsonl")
ANCHOR_ID = "LCL-17"
EXPECTED_IDS = [f"MT-{i}" for i in range(1, 10)]


def load_statute_texts(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[str(row["statute_id"])] = str(row["text"])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--specs", default=str(SPECS_PATH))
    ap.add_argument("--statutes", default=str(STATUTES_PATH))
    args = ap.parse_args(argv)

    try:
        specs = load_missing_terms(args.specs)
    except (OSError, ValueError) as exc:
        print(f"[FAIL] 规格表读取失败：{exc}")
        return 1

    statutes = load_statute_texts(Path(args.statutes))
    anchor = statutes.get(ANCHOR_ID, "")
    errors: list[str] = []

    ids = [s.term_id for s in specs]
    if ids != EXPECTED_IDS:
        errors.append(f"term_id 必须恰为 MT-1..MT-9（有序且唯一），实际：{' / '.join(ids)}")

    for spec in specs:
        text = statutes.get(spec.statute_id)
        if text is None:
            errors.append(f"{spec.term_id}: 法条库中不存在 {spec.statute_id}")
        if not anchor:
            errors.append(f"法条库中缺少 {ANCHOR_ID}，无法核对依据片段")
        elif spec.evidence_span not in anchor:
            errors.append(f"{spec.term_id}: evidence_span 不是 {ANCHOR_ID} 原文子串（{spec.evidence_span}）")

    if errors:
        print(f"[FAIL] {len(errors)} 个问题：")
        for err in errors:
            print("  -", err)
        return 1
    print(f"[OK] {len(specs)} 条规格合法，依据片段全部命中 {ANCHOR_ID} 原文")
    return 0


if __name__ == "__main__":
    sys.exit(main())
