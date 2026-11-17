"""命令行冒烟：`check` 子命令能跑通、`--version` 有输出、未知子命令返回非零。

`init` 的口令策略见 test_cli_init.py，这里只补 CLI 入口本身。
全部走进程内 `main([...])`：conftest 已把抽取固定为纯正则档并屏蔽 Key，不联网。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from mla import __version__
from mla.cli import cmd_check, main
from mla.config import data_path, mla_home
from mla.services.tasks import NO_MODEL_MESSAGE

DUMMY_KEY = "sk-dummy-" + "0" * 26
OUT_OF_SCOPE = "离婚时房产怎么分割"


def _legacy_db(path: Path, key: str = DUMMY_KEY) -> Path:
    """造一份带 `settings.llm_api_key` 的哑旧库（不含任何真实 Key）。"""
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)")
        conn.execute("INSERT INTO settings (key, value) VALUES ('llm_api_key', ?)", (key,))
    return path


def _check_args(**overrides: object) -> argparse.Namespace:
    """`cmd_check` 需要的最小参数集（越界问题不会调用模型，不联网）。"""
    fields: dict[str, object] = {
        "rules": None,
        "facts": None,
        "contract": None,
        "question": OUT_OF_SCOPE,
        "task_id": "T-0001",
        "variant": "full",
        "trace": False,
    }
    fields.update(overrides)
    return argparse.Namespace(**fields)


def test_check_runs_contract_and_prints_json(capsys: pytest.CaptureFixture[str]) -> None:
    """核对子命令跑通整条图，输出可解析的 JSON，节点顺序可审计。"""
    code = main(["check", "--contract", data_path("evalsets/sample_contract.txt"), "--trace"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == __version__
    assert payload["rules_loaded"] > 0
    assert payload["boundary"]["in_scope"] is True
    assert payload["verify"] == {"ok": True, "degraded": False}
    # --trace 给出真实经过的节点顺序：首节点 intake、末节点 output、必定经过 retrieve
    order = payload["node_order"]
    assert order[0] == "intake" and order[-1] == "output"
    assert "retrieve" in order and "verify" in order


def test_check_refuses_out_of_scope_question(capsys: pytest.CaptureFixture[str]) -> None:
    """越界问题走拒答分支：不产出任何结论；拒答是正常结果，退出码仍为 0。"""
    code = main(["check", "--question", "离婚时房产怎么分割"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["boundary"]["in_scope"] is False
    assert payload["boundary"]["layer"] == "terms"
    assert payload["items"] == []
    assert payload["retrieved"] == []  # 拒答分支不进检索
    assert any("越界拒答" in e for e in payload["errors"])


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    """`--version` 打印版本号后正常退出（argparse 用 SystemExit(0) 结束）。"""
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_unknown_subcommand_exits_non_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """未知子命令由 argparse 拒绝，返回非零退出码而不是静默成功。"""
    with pytest.raises(SystemExit) as excinfo:
        main(["no-such-command"])
    assert excinfo.value.code not in (0, None)
    assert "no-such-command" in capsys.readouterr().err


# ---------- Key 解析：统一 resolver（env > auth.json > 阶段三旧库兜底） ----------


def test_cmd_check_without_any_key_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """没有任何 Key 时门禁照旧：提示指向设置页、退出码 2、不产出结论。"""
    monkeypatch.delenv("MLA_ALLOW_NO_LLM", raising=False)

    code = main(["check", "--question", OUT_OF_SCOPE])

    assert code == 2
    captured = capsys.readouterr()
    assert NO_MODEL_MESSAGE in captured.err
    assert captured.out == ""


def test_cmd_check_reads_auth_file_via_resolver(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """只有 auth.json 有 Key 时门禁放行：cli.py 不再自己读数据库，也不再泄漏 Key。"""
    monkeypatch.delenv("MLA_ALLOW_NO_LLM", raising=False)
    # 先证明无 Key 时确实被拦下（同一隔离环境）
    assert main(["check", "--question", OUT_OF_SCOPE]) == 2
    capsys.readouterr()

    home = mla_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(
        json.dumps({"deepseek": {"type": "api_key", "key": DUMMY_KEY}}), encoding="utf-8"
    )

    assert main(["check", "--question", OUT_OF_SCOPE]) == 0
    captured = capsys.readouterr()
    assert DUMMY_KEY not in captured.out + captured.err


def test_cmd_check_no_longer_reads_key_from_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """阶段四删除旧库兜底：库里有 Key 也不再让门禁放行（Key 只能来自 env / auth.json）。"""
    legacy = _legacy_db(tmp_path / "cwd-old.sqlite3")
    monkeypatch.delenv("MLA_ALLOW_NO_LLM", raising=False)

    assert cmd_check(_check_args(runtime_db_path=str(legacy))) == 2
    assert NO_MODEL_MESSAGE in capsys.readouterr().err
