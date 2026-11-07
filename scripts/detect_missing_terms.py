"""必备条款缺项检测调试打印（纯规则，不联网、不调模型）。

用法：
    python scripts/detect_missing_terms.py data/evalsets/contract_traps_03.txt   # 单份合同逐类判定
    python scripts/detect_missing_terms.py                                      # 4 份基线合同出矩阵表
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mla.config import load_config
from mla.domain.missing import detect_missing_terms, load_missing_terms
from mla.domain.models import CheckTask, MissingStatus, MissingTerm, TaskMode

EVAL_DIR = Path("data/evalsets")
DEFAULT_CONTRACTS = (
    "contract_compliant_01.txt",
    "contract_traps_01.txt",
    "contract_traps_02.txt",
    "contract_traps_03.txt",
)
CELL = {
    MissingStatus.WRITTEN: "已写",
    MissingStatus.ABSENT: "缺失",
    MissingStatus.UNSEEN: "未见",
    MissingStatus.MANUAL: "人工",
}
CELL_WIDTH = 6
STATUS_WIDTH = 10


def _width(text: str) -> int:
    """终端显示宽度：中日韩字符按 2 列算。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _coverage(terms: tuple[MissingTerm, ...]) -> float | None:
    """沿用任务口径（已写 / 8，第九类不判）；不另写一套算法。"""
    return CheckTask(task_id="T-detector", mode=TaskMode.CHECK, missing_terms=list(terms)).missing_coverage


def _fmt_coverage(value: float | None) -> str:
    return "无" if value is None else f"{value:.3f}"


def _detect(path: Path, min_chars: int) -> tuple[str, tuple[MissingTerm, ...]]:
    text = path.read_text(encoding="utf-8")
    specs = load_missing_terms(load_config().paths.missing_terms)
    return text, detect_missing_terms(text, specs, min_chars=min_chars)


def _print_single(path: Path, min_chars: int) -> None:
    text, terms = _detect(path, min_chars)
    print(f"合同：{path.name}（{len(text)} 字，阈值 {min_chars}）")
    for term in terms:
        print(
            f"  {term.term_id:<6}{_pad(str(term.status), STATUS_WIDTH)}"
            f"{term.name}　{term.clause_no}·{term.statute_id}"
        )
        if term.note:
            print(f"        备注：{term.note}")
    written = sum(1 for t in terms if t.status == MissingStatus.WRITTEN)
    coverage = _coverage(terms)
    reason = "，全部需人工确认" if coverage is None else ""
    print(f"\n覆盖率：{_fmt_coverage(coverage)}{reason}（已写 {written} / 8；MT-9 不判）")
    absent = [t.term_id for t in terms if t.status == MissingStatus.ABSENT]
    unseen = [t.term_id for t in terms if t.status == MissingStatus.UNSEEN]
    print(f"缺失：{'、'.join(absent) if absent else '无'}")
    print(f"未见（请人工确认）：{'、'.join(unseen) if unseen else '无'}")


def _print_matrix(paths: list[Path], min_chars: int) -> None:
    rows: list[tuple[str, tuple[MissingTerm, ...]]] = [(p.name, _detect(p, min_chars)[1]) for p in paths]
    term_ids = [t.term_id for t in rows[0][1]] if rows else []
    name_width = max((_width(name) for name, _ in rows), default=10) + 4
    header = _pad("合同", name_width) + "".join(_pad(tid, CELL_WIDTH) for tid in term_ids) + "覆盖率"
    print(f"（阈值 {min_chars} 字；单元格：已写 / 缺失 / 未见 / 人工=需人工确认）")
    print(header)
    for name, terms in rows:
        cells = "".join(_pad(CELL.get(t.status, str(t.status)), CELL_WIDTH) for t in terms)
        print(_pad(name, name_width) + cells + _fmt_coverage(_coverage(terms)))
    print("\n注：MT-9 是兜底条款，一律「人工」，不进覆盖率分母。")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "contract", nargs="?", default="", help="合同文本路径（UTF-8）；省略则批量跑 4 份基线合同"
    )
    ap.add_argument("--min-chars", type=int, default=None, help="短文阈值，默认取配置")
    args = ap.parse_args(argv)

    min_chars = args.min_chars if args.min_chars is not None else load_config().missing_terms.min_chars
    if args.contract:
        path = Path(args.contract)
        if not path.is_file():
            print(f"[FAIL] 合同文件不存在：{path}", file=sys.stderr)
            return 1
        _print_single(path, min_chars)
        return 0

    paths = [EVAL_DIR / name for name in DEFAULT_CONTRACTS]
    missing = [p for p in paths if not p.is_file()]
    if missing:
        print(f"[FAIL] 基线合同缺失：{'、'.join(str(p) for p in missing)}", file=sys.stderr)
        return 1
    _print_matrix(paths, min_chars)
    return 0


if __name__ == "__main__":
    sys.exit(main())
