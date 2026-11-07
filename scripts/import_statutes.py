"""法源入库：把外部采集的原始 jsonl 规范化后写入 data/statutes/statutes.jsonl。

规范化规则：
- `expiry_date` / `superseded_by` / `effective_date` 的空字符串 -> null（代码里是 date|None / str|None）
- 丢弃空行；按 statute_id 前缀 + 条号排序，便于人工核对
- 不修改原文（text 原样写入）

用法：
    python scripts/import_statutes.py <源 jsonl> [--dest data/statutes/statutes.jsonl]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

NORM_NULL = ("expiry_date", "superseded_by")


def normalize(row: dict) -> dict:
    out = dict(row)
    for key in NORM_NULL:
        if isinstance(out.get(key), str) and not out[key].strip():
            out[key] = None
    if isinstance(out.get("effective_date"), str) and not out["effective_date"].strip():
        out["effective_date"] = None
    out["region"] = out.get("region") or "全国"
    return out


def sort_key(row: dict) -> tuple[str, int]:
    sid = str(row.get("statute_id", ""))
    prefix, _, num = sid.partition("-")
    digits = "".join(ch for ch in num if ch.isdigit())
    return prefix, int(digits) if digits else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--dest", default="data/statutes/statutes.jsonl")
    args = ap.parse_args(argv)

    src = Path(args.source)
    if not src.exists():
        print(f"源文件不存在：{src}")
        return 1

    rows: list[dict] = []
    for i, line in enumerate(src.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(normalize(json.loads(line)))
        except json.JSONDecodeError as exc:
            print(f"第 {i} 行 JSON 解析失败：{exc}")
            return 1

    rows.sort(key=sort_key)
    dest = Path(args.dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    from collections import Counter

    counts = Counter(str(r["statute_id"]).split("-")[0] for r in rows)
    print(f"写入 {dest}：{len(rows)} 条")
    print("分法源：" + "，".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
