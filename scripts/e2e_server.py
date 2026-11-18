"""E2E 后端启动器：打桩模型 + 临时运行目录，供 Playwright 驱动一个真实进程。

为什么需要这个脚本（006 批次 5「E2E」）：

- 真实核对需要模型，但 E2E 不能联网、也不该读本机真实 Key：这里把
  `mla.services.tasks.DeepSeekLLM` 换成本文件里的打桩生成器（思路与
  `scripts/benchmark_frontend.py` 相同），图 / 规则 / 条款审查 / 共识层仍然完整执行；
- 没有 Key 时 `model_configured=False`，前端会正确地挡在「未配置模型」门禁上，
  所以「有模型」这一路要注入一个**明显是占位**的 Key，并用 `--no-model` 起第二个进程
  专门验证门禁；
- 运行目录（`MLA_HOME` / `MLA_DB` / `MLA_INDEX_DIR`）指向 `--home` 并在启动时清空，
  `/setup/admin` 因此每次都能重新创建管理员，测试之间互不污染。

用法（一般由 `web/playwright.config.ts` 的 `webServer` 调用）：

    python scripts/e2e_server.py --port 8021 --home _tmp_e2e/model
    python scripts/e2e_server.py --port 8022 --home _tmp_e2e/no-model --no-model
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

#: 占位 Key：只用来让「有没有模型」的门禁放行，不会发出任何真实请求（生成器被打桩）。
FAKE_KEY = "e2e-key-not-a-real-credential"


class StubGenerator:
    """打桩生成器：条款审查逐条回 C 级、处理建议回空，其余一律空对象。

    C 级 + 空 `statute_ids` 是核验层允许的最省路径，但仍然走完「候选 → 判定 → 核验 →
    共识 → 条目」的编排，所以 E2E 看到的是真实结构、真实文案，不是写死的假 DTO。
    """

    available = True
    usage_in = 0
    usage_out = 0

    def generate(self, system: str, user: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        if "条款风险审查" in system:
            try:
                payload = json.loads(user)
            except ValueError:
                return {"findings": [], "extra": []}
            findings = [
                {
                    "idx": idx,
                    "grade": "C",
                    "reason": "E2E 打桩理由",
                    "statute_ids": [],
                    "evidence": candidate["span"],
                    "need_facts": [],
                }
                for idx, candidate in enumerate(payload.get("candidates") or [])
            ]
            return {"findings": findings, "extra": []}
        if "合规顾问" in system:
            return {"suggestions": []}
        return {}

    def classify(self, text: str, labels: list[str]) -> str:
        return labels[0] if labels else "不确定"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="E2E 用后端（打桩模型 + 临时目录）")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--home", required=True, help="运行目录（启动时清空）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--no-model",
        action="store_true",
        help="不注入 Key：用于验证「未配置模型」门禁",
    )
    return parser.parse_args(argv)


def prepare_environment(home: Path, *, no_model: bool) -> None:
    shutil.rmtree(home, ignore_errors=True)
    (home / "index").mkdir(parents=True)
    os.environ["MLA_HOME"] = str(home)
    os.environ["MLA_DB"] = str(home / "mla-e2e.sqlite3")
    os.environ["MLA_INDEX_DIR"] = str(home / "index")
    # 抽取走正则档：不联网也能拿到与旧路径一致的候选条款
    os.environ["MLA_EXTRACT_MODE"] = "regex"
    os.environ["MLA_MODEL_FIRST"] = "on"
    os.environ.pop("MLA_ALLOW_NO_LLM", None)
    os.environ.pop("DASHSCOPE_API_KEY", None)
    if no_model:
        os.environ.pop("MLA_LLM_API_KEY", None)
    else:
        os.environ["MLA_LLM_API_KEY"] = FAKE_KEY


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    prepare_environment(Path(args.home).resolve(), no_model=args.no_model)

    import uvicorn

    from mla.api.app import create_app

    if not args.no_model:
        import mla.services.tasks as tasks

        stub = StubGenerator()
        tasks.DeepSeekLLM = lambda cfg: stub  # type: ignore[assignment]

    uvicorn.run(
        create_app(),
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
