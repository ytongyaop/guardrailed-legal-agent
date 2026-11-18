"""界面文案门禁：检查中文文案的字符与标点是否统一（简体、无错乱符号）。

用法：python scripts/check_ui_text.py
退出码 1 表示有违规，需修完再提交。

0.4.0（规格 006 批次 6）起界面是 Vue + FastAPI，扫描面扩到前端源码：`web/src/**/*.{vue,ts,css}`。
规则与 Python 侧共用一张表，前端侧只是换了取文本的方式（逐行、跳注释）——不再有
`web/tests/ui-text.spec.ts` 那份平行的第二套标准：

- Python：只取字符串常量（`ast`），注释与标识符天然不进扫描；
- 前端：逐行扫，跳过 `//`、`/*`、`*`、`<!--` 开头的注释行与生成文件 `components.d.ts`；
- `node_modules` / `dist` 不在 `web/src` 下，天然不扫。

`连续英文句点` 只对含中文的行生效：JS/TS 的展开运算符 `...` 不是文案问题。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

#: Python 侧的文案来源（界面模块已随 0.4.0 删除，剩报告与评测脚本）
PY_FILES = (
    "src/mla/services/report.py",
    "scripts/run_eval.py",
)

#: 前端源码根目录与扫描的扩展名（与旧 `web/tests/ui-text.spec.ts` 同一范围，另加 `.css`）
WEB_ROOT = Path("web/src")
WEB_EXTS = (".vue", ".ts", ".css")

#: 构建/类型生成物：内容不是人手写的文案，扫它只会制造噪声
WEB_SKIP = {"components.d.ts"}

CJK = r"[\u4e00-\u9fff]"
CJK_PATTERN = re.compile(CJK)

TRADITIONAL = set(
    "個們這那麼與為說時會來對開關題實現應該當發現長門馬鳥龍龜臺灣萬專業務員經濟學習練體認識讓從無邊進過還樣幾頭"
)

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("替换字符", re.compile(r"\ufffd")),
    ("全角空格", re.compile(r"\u3000")),
    ("直角引号", re.compile(r"[「」『』]")),
    ("emoji 勾叉", re.compile(r"[✅❌✓✗]")),
    ("单个省略号", re.compile(r"(?<!\u2026)\u2026(?!\u2026)")),
    ("中文间半角逗号", re.compile(CJK + r",(?=" + CJK + ")")),
    ("中文间半角分号", re.compile(CJK + r";(?=" + CJK + ")")),
    ("中文间半角冒号", re.compile(CJK + r":(?=" + CJK + ")")),
    ("中文后半角括号", re.compile(CJK + r"[()]")),
    ("中文旁竖线", re.compile(CJK + r"\s*[|｜]\s*(?=" + CJK + ")")),
)

#: 只对含中文的行生效：`...` 在前端多是展开运算符
CJK_ONLY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (("连续英文句点", re.compile(r"\.\.\.")),)

EXTRA_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("繁体字", re.compile("[" + "".join(sorted(TRADITIONAL)) + "]")),
)


def chinese_strings(path: Path):
    """Python：只取含中文的字符串常量，按行给出（行号为常量起始行 + 偏移）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if CJK_PATTERN.search(node.value):
                yield node.lineno, node.value


def web_lines(path: Path):
    """前端：逐行给出非注释、非空的行（注释行按首个非空字符判断，与旧 spec 一致）。"""
    text = path.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("//", "/*", "*", "<!--")):
            continue
        yield lineno, line


def check_line(text: str) -> list[str]:
    """返回该行的违规标签；`连续英文句点` 只在含中文时判。"""
    has_cjk = CJK_PATTERN.search(text) is not None
    hits = [label for label, rule in (*RULES, *EXTRA_RULES) if rule.search(text)]
    if has_cjk:
        hits.extend(label for label, rule in CJK_ONLY_RULES if rule.search(text))
    return hits


def check_python(name: str) -> list[str]:
    problems: list[str] = []
    for lineno, raw in chinese_strings(Path(name)):
        for offset, line in enumerate(raw.splitlines(), start=1):
            text = line.strip()
            if not text:
                continue
            for label in check_line(text):
                problems.append(f"{name}:{lineno + offset - 1} [{label}] {text[:80]}")
    return problems


def check_web() -> list[str]:
    problems: list[str] = []
    if not WEB_ROOT.is_dir():
        return problems
    for path in sorted(WEB_ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in WEB_EXTS:
            continue
        if path.name in WEB_SKIP:
            continue
        for lineno, line in web_lines(path):
            for label in check_line(line):
                problems.append(f"{path.as_posix()}:{lineno} [{label}] {line.strip()[:80]}")
    return problems


def main() -> int:
    problems: list[str] = []
    for name in PY_FILES:
        if Path(name).exists():
            problems.extend(check_python(name))
    problems.extend(check_web())

    if problems:
        print(f"发现 {len(problems)} 处文案问题：")
        for item in problems:
            print("  - " + item)
        return 1
    print("文案检查通过：简体、标点与符号统一（Python 文案 + web/src 前端源码）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
