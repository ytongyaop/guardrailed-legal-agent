"""界面文案门禁：检查中文文案的字符与标点是否统一（简体、无错乱符号）。

用法：python scripts/check_ui_text.py
退出码 1 表示有违规，需修完再提交。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

FILES = (
    "src/mla/ui/app.py",
    "src/mla/ui/trend.py",
    "src/mla/services/report.py",
    "scripts/run_eval.py",
)

CJK = r"[\u4e00-\u9fff]"
TRADITIONAL = set(
    "個們這那麼與為說時會來對開關題實現應該當發現長門馬鳥龍龜臺灣萬專業務員經濟學習練體認識讓從無邊進過還樣幾頭"
)

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("替换字符", re.compile(r"\ufffd")),
    ("全角空格", re.compile(r"\u3000")),
    ("直角引号", re.compile(r"[「」『』]")),
    ("emoji 勾叉", re.compile(r"[✅❌✓✗]")),
    ("单个省略号", re.compile(r"(?<!…)…(?!…)")),
    ("中文间半角逗号", re.compile(CJK + r",(?=" + CJK + ")")),
    ("中文间半角分号", re.compile(CJK + r";(?=" + CJK + ")")),
    ("中文间半角冒号", re.compile(CJK + r":(?=" + CJK + ")")),
    ("中文后半角括号", re.compile(CJK + r"[()]")),
    ("中文旁竖线", re.compile(CJK + r"\s*[|｜]\s*(?=" + CJK + ")")),
    ("连续英文句点", re.compile(r"\.\.\.")),
)

EXTRA_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("繁体字", re.compile("[" + "".join(sorted(TRADITIONAL)) + "]")),
)


def chinese_strings(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if re.search(CJK, node.value):
                yield node.lineno, node.value


def main() -> int:
    problems: list[str] = []
    for name in FILES:
        path = Path(name)
        if not path.exists():
            continue
        for lineno, raw in chinese_strings(path):
            for line_no, line in enumerate(raw.splitlines(), start=1):
                text = line.strip()
                if not text:
                    continue
                for label, rule in (*RULES, *EXTRA_RULES):
                    if rule.search(text):
                        problems.append(f"{name}:{lineno + line_no - 1} [{label}] {text[:80]}")

    if problems:
        print(f"发现 {len(problems)} 处文案问题：")
        for item in problems:
            print("  - " + item)
        return 1
    print("文案检查通过：简体、标点与符号统一。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
