"""法源库校验器。

用法：
    python scripts/validate_statutes.py <statutes.jsonl> [--snapshot-dir DIR] [--min 110]

检查项：
1. 每行是合法 JSON、13 个字段齐全、关键字段非空
2. statute_id 唯一、格式合法
3. source_url 只允许四个官方域
4. text 长度 >= 20，且不含占位词
5. 与本地官方快照**逐字**比对（去空白后做子串匹配）
6. 按法源前缀统计条数

退出码：0=全部通过；1=有错误（错误逐条打印）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

REQUIRED = [
    "statute_id",
    "law_name",
    "article_no",
    "text",
    "issuing_body",
    "effective_date",
    "expiry_date",
    "status",
    "superseded_by",
    "level",
    "region",
    "source_url",
]
ALLOWED_DOMAINS = ("flk.npc.gov.cn", "gov.cn", "mohrss.gov.cn", "court.gov.cn")
PLACEHOLDERS = ("略", "参见", "同上", "……略", "（略）")
ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-[0-9A-Z]+$")

# 前缀 -> 文件名关键词（在快照目录里按关键词匹配，避免硬编码目录名）
SNAPSHOT_KEYS: dict[str, tuple[str, ...]] = {
    "LCL": ("劳动合同法 labor", "劳动合同法.md"),
    "LCR": ("劳动合同法实施条例",),
    "LL": ("01-劳动法",),
    "J1": ("解释一",),
    "J2": ("解释二",),
    "LDA": ("02-劳动争议调解仲裁法", "调解仲裁法.md"),
    "SIL": ("社会保险法",),
    "WI": ("工伤保险条例",),
    "WPR": ("工资支付暂行规定",),
    "AL": ("职工带薪年休假条例",),
    "ALI": ("年休假实施办法",),
}


def norm(s: str) -> str:
    """去空白 + 统一常见标点，便于逐字比对（只容空白差异，不容改字）。"""
    s = re.sub(r"\s+", "", s)
    for a, b in (("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'"), ("（", "("), ("）", ")")):
        s = s.replace(a, b)
    return s


def load_rows(path: Path) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    rows: list[dict] = []
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        errors.append("文件带 BOM（应为无 BOM）")
    for i, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"第 {i} 行不是合法 JSON：{exc}")
            continue
        if not isinstance(obj, dict):
            errors.append(f"第 {i} 行不是对象")
            continue
        rows.append(obj)
    return rows, errors


def check_rows(rows: list[dict]) -> list[str]:
    errors: list[str] = []
    ids: Counter[str] = Counter()
    for idx, row in enumerate(rows, start=1):
        missing = [k for k in REQUIRED if k not in row]
        if missing:
            errors.append(f"#{idx} 缺字段 {missing}")
            continue
        sid = str(row["statute_id"])
        ids[sid] += 1
        if not ID_RE.match(sid):
            errors.append(f"#{idx} statute_id 格式异常：{sid}")
        for k in (
            "law_name",
            "article_no",
            "text",
            "issuing_body",
            "status",
            "level",
            "region",
            "source_url",
        ):
            if not str(row.get(k, "")).strip():
                errors.append(f"#{idx} 字段为空：{k}")
        text = str(row["text"])
        if len(text) < 20:
            errors.append(f"#{idx}（{sid}）text 过短（{len(text)} 字）")
        for ph in PLACEHOLDERS:
            if ph in text:
                errors.append(f"#{idx}（{sid}）text 含占位词「{ph}」")
        url = str(row["source_url"])
        if not any(d in url for d in ALLOWED_DOMAINS):
            errors.append(f"#{idx}（{sid}）source_url 非官方域：{url[:60]}")
    for sid, n in ids.items():
        if n > 1:
            errors.append(f"statute_id 重复 {n} 次：{sid}")
    return errors


def check_against_snapshots(rows: list[dict], snap_dir: Path, extra: dict[str, str]) -> list[str]:
    """逐字核对：条文（去空白）必须出现在官方快照里。"""
    errors: list[str] = []
    cache: dict[str, str] = {}
    used: dict[str, str] = {}

    def snapshot_text(prefix: str) -> str | None:
        if prefix in cache:
            return cache[prefix]
        if prefix in extra:
            p = Path(extra[prefix])
            if p.exists():
                cache[prefix] = norm(p.read_text(encoding="utf-8", errors="ignore"))
                return cache[prefix]
        for key in SNAPSHOT_KEYS.get(prefix, ()):
            matches = sorted(snap_dir.glob(f"**/*{key}*"))
            md = [m for m in matches if m.suffix.lower() in (".md", ".txt")]
            if md:
                best = max(md, key=lambda m: m.stat().st_size)  # 取最完整的那份（摘要 vs 全文）
                cache[prefix] = norm(best.read_text(encoding="utf-8", errors="ignore"))
                used[prefix] = str(best)
                return cache[prefix]
        cache[prefix] = ""
        return None

    compared: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    for row in rows:
        sid = str(row["statute_id"])
        prefix = sid.split("-")[0]
        snap = snapshot_text(prefix)
        if not snap:
            skipped[prefix] += 1
            continue
        compared[prefix] += 1
        if norm(str(row["text"])) not in snap:
            errors.append(f"{sid}：text 与官方快照**不逐字一致**（可能改字/漏字/多字）")

    print("逐字核对覆盖：" + "，".join(f"{k}={v}" for k, v in sorted(compared.items())))
    if used:
        print("比对文件：")
        for k, v in sorted(used.items()):
            print(f"  {k} -> {Path(v).name}")
    if skipped:
        print("⚠ 无快照可比对（未覆盖）：" + "，".join(f"{k}={v}" for k, v in sorted(skipped.items())))
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl")
    ap.add_argument(
        "--snapshot-dir",
        default=None,
        help="官方原文快照根目录（按法源前缀分子目录）；不提供则跳过逐字比对",
    )
    ap.add_argument(
        "--extra",
        action="append",
        default=[],
        help="补充分组快照，格式 前缀=路径",
    )
    ap.add_argument("--min", type=int, default=110)
    args = ap.parse_args(argv)

    path = Path(args.jsonl)
    if not path.exists():
        print(f"文件不存在：{path}")
        return 1

    rows, errors = load_rows(path)
    errors += check_rows(rows)
    extra = {}
    for item in args.extra:
        if "=" in item:
            k, v = item.split("=", 1)
            extra[k] = v
    if args.snapshot_dir or extra:
        # 只给 --extra 时无快照根目录可用，传一个不存在的占位目录（glob 空结果，不会误命中）
        snap_dir = Path(args.snapshot_dir) if args.snapshot_dir else Path("__no_snapshot__")
        errors += check_against_snapshots(rows, snap_dir, extra)
    else:
        print("未提供官方快照目录（--snapshot-dir），跳过逐字比对，仅做结构与条数校验。")

    counts = Counter(str(r["statute_id"]).split("-")[0] for r in rows if "statute_id" in r)
    print(f"总数：{len(rows)} 条（验收线 ≥{args.min}）")
    print("分法源：" + "，".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if len(rows) < args.min:
        errors.append(f"条数不足：{len(rows)} < {args.min}")

    if errors:
        print(f"\n[FAIL] 发现 {len(errors)} 个问题：")
        for e in errors[:60]:
            print("  -", e)
        return 1

    print("\n[OK] 全部检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
