"""要素骨架校验器（L2 辅助层，规格 003 第 4.3 节 / 验收 V19）。

检查项：
1. 13 个要素、`element_id` 唯一且为 `EL-1`..`EL-13`（文件顺序即劳动合同的叙述顺序）
2. `name` 非空；`impact` ∈ {高, 中, 低}
3. `section_hints` 2–5 个非空字符串（只用于段落归类，不是写法穷举）
4. `focus` 3–5 条、每条 8–40 字
5. **禁数字**：`name` 与 `focus` 不得出现"数字＋单位"（月 / 天 / 年 / % / 倍 / 元 / 小时）
6. **禁条号**：不得出现 `第N条`（含中文数字）、`LCL-` / `LL-` 这类法条 ID
7. **禁枚举引导**：不得出现 `包括但不限于` / `等等` / `等表述`

第 5–7 条是这个文件存在的理由：数值随地区实施细则与法律修订变化，写死在提示层＝每次修订
都要改全表；条号只允许出现在 L1（必须引用且过原文核验）与 L3（在库 / 闭集校验）；
写法枚举永远追不完，还会把模型推向"按清单找字面"而不是"读合同结构"。

用法：
    python scripts/validate_elements.py [--elements PATH]
失败退出码 1（任一结构问题或禁用形态）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mla.config import data_path  # noqa: E402

ELEMENTS_PATH = Path(data_path("clauses/elements.yaml"))
EXPECTED_IDS: tuple[str, ...] = tuple(f"EL-{i}" for i in range(1, 14))
IMPACTS = ("高", "中", "低")
MIN_FOCUS, MAX_FOCUS = 3, 5
MIN_FOCUS_CHARS, MAX_FOCUS_CHARS = 8, 40
MIN_HINTS, MAX_HINTS = 2, 5
_REQUIRED = ("element_id", "name", "section_hints", "impact", "focus")

NUMERIC_UNIT_RE = re.compile(r"\d+\s*(个月|月|天|日|年|%|倍|元|小时)")
ARTICLE_NO_RE = re.compile(r"第\s*[0-9一二三四五六七八九十百]+\s*条")
STATUTE_ID_RE = re.compile(r"(?:LCL|LL)-[A-Z0-9]+")
ENUM_LEAD_RE = re.compile(r"包括但不限于|等等|等表述")


def _texts(row: dict[str, Any]) -> list[tuple[str, str]]:
    """要过禁用形态检查的文本：（字段名, 文本）。`section_hints` 也查——它是给人读的特征词。"""
    pairs: list[tuple[str, str]] = [("name", str(row.get("name", "")))]
    pairs += [("focus", str(x)) for x in (row.get("focus") or []) if isinstance(x, str)]
    pairs += [("section_hints", str(x)) for x in (row.get("section_hints") or []) if isinstance(x, str)]
    return pairs


def _check_row(index: int, row: object, errors: list[str]) -> dict[str, Any] | None:
    """逐条结构校验；结构坏到无法给 ID 时返回 None（已记错误）。"""
    if not isinstance(row, dict):
        errors.append(f"第 {index} 个要素不是对象")
        return None
    eid = str(row.get("element_id", "")).strip()
    ctx = f"第 {index} 个要素（{eid or '无 ID'}）"
    missing = [key for key in _REQUIRED if key not in row]
    if missing:
        errors.append(f"{ctx}：缺字段 {' / '.join(missing)}")
        return None

    if not str(row["name"]).strip():
        errors.append(f"{ctx}：name 为空")
    if str(row["impact"]) not in IMPACTS:
        errors.append(f"{ctx}：impact={row['impact']}，必须是 {' / '.join(IMPACTS)}")

    hints = row["section_hints"]
    if not isinstance(hints, list) or any(not isinstance(x, str) or not x.strip() for x in hints):
        errors.append(f"{ctx}：section_hints 必须是非空字符串列表")
    elif not MIN_HINTS <= len(hints) <= MAX_HINTS:
        errors.append(f"{ctx}：section_hints {len(hints)} 个，要求 {MIN_HINTS}–{MAX_HINTS} 个")

    focus = row["focus"]
    if not isinstance(focus, list) or any(not isinstance(x, str) for x in focus):
        errors.append(f"{ctx}：focus 必须是字符串列表")
    else:
        if not MIN_FOCUS <= len(focus) <= MAX_FOCUS:
            errors.append(f"{ctx}：focus {len(focus)} 条，要求 {MIN_FOCUS}–{MAX_FOCUS} 条")
        for item in focus:
            length = len(item.strip())
            if not MIN_FOCUS_CHARS <= length <= MAX_FOCUS_CHARS:
                errors.append(
                    f"{ctx}：focus 单条 {length} 字，要求 {MIN_FOCUS_CHARS}–{MAX_FOCUS_CHARS} 字：{item}"
                )

    for field, text in _texts(row):
        for label, pattern in (
            ("禁数字", NUMERIC_UNIT_RE),
            ("禁条号", ARTICLE_NO_RE),
            ("禁法条 ID", STATUTE_ID_RE),
            ("禁枚举引导", ENUM_LEAD_RE),
        ):
            hit = pattern.search(text)
            if hit:
                errors.append(f"{ctx}：{field} 出现{label}（{hit.group(0)}）：{text}")
    return row


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--elements", default=str(ELEMENTS_PATH))
    args = ap.parse_args(argv)

    path = Path(args.elements)
    if not path.is_file():
        print(f"[FAIL] 要素骨架不存在：{path}")
        return 1
    try:
        rows = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except yaml.YAMLError as exc:
        print(f"[FAIL] 要素骨架不是合法 YAML：{exc}")
        return 1
    if not isinstance(rows, list):
        print("[FAIL] 要素骨架顶层必须是列表")
        return 1

    errors: list[str] = []
    ids: list[str] = []
    for index, row in enumerate(rows, start=1):
        checked = _check_row(index, row, errors)
        if checked is not None:
            ids.append(str(checked.get("element_id", "")).strip())

    if len(rows) != len(EXPECTED_IDS):
        errors.append(f"要素 {len(rows)} 个，要求 {len(EXPECTED_IDS)} 个")

    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        errors.append(f"element_id 重复：{' / '.join(dupes)}")
    unknown = [i for i in ids if i not in EXPECTED_IDS]
    if unknown:
        errors.append(f"element_id 不在约定范围内：{' / '.join(unknown)}")
    missing_ids = [i for i in EXPECTED_IDS if i not in ids]
    if missing_ids:
        errors.append(f"缺少要素：{' / '.join(missing_ids)}")
    if not dupes and not unknown and not missing_ids and tuple(ids) != EXPECTED_IDS:
        errors.append(f"要素顺序应与叙述顺序一致（{' / '.join(EXPECTED_IDS)}），实测 {' / '.join(ids)}")

    print(f"要素骨架：{path}")
    print(f"  · {len(rows)} 个要素（要求 {len(EXPECTED_IDS)} 个）")
    for row in rows:
        if not isinstance(row, dict):
            continue
        focus_count = len(row.get("focus") or []) if isinstance(row.get("focus"), list) else 0
        hint_count = len(row.get("section_hints") or []) if isinstance(row.get("section_hints"), list) else 0
        print(
            f"  · {row.get('element_id', '?')} {row.get('name', '?')}"
            f"（{row.get('impact', '?')}）：focus {focus_count} 条 / 特征词 {hint_count} 个"
        )

    if errors:
        print(f"\n[FAIL] {len(errors)} 个问题：")
        for item in errors:
            print("  -", item)
        return 1
    print(
        "\n[OK] 要素骨架结构合法：13 个要素、影响度取值合法、focus 条数与长度合规，且不含数值、条号与写法枚举"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
