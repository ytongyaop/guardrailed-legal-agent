"""命令行冒烟：`check` 子命令能跑通、`--version` 有输出、未知子命令返回非零。

`init` 的口令策略见 test_cli_init.py，这里只补 CLI 入口本身。
全部走进程内 `main([...])`：conftest 已把抽取固定为纯正则档并屏蔽 Key，不联网。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mla import __version__
from mla.cli import main
from mla.config import data_path


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


# ---------- 数据库里的 Key 兜底（界面设置页写 settings 表） ----------


def test_sqlite_api_key_reads_setting_from_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """界面把 Key 存进 settings 表的 llm_api_key，命令行的兜底读取要取到同一份。"""
    from mla.adapters.persist import SqliteRepository
    from mla.cli import _sqlite_api_key

    db = tmp_path / "key.sqlite3"
    SqliteRepository(db_path=str(db)).set_setting("llm_api_key", "sk-local-test")
    monkeypatch.setenv("MLA_DB", str(db))
    assert _sqlite_api_key() == "sk-local-test"


def test_sqlite_api_key_empty_when_db_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """库文件不存在时返回空串：读配置不创建数据库。"""
    from mla.cli import _sqlite_api_key

    db = tmp_path / "missing.sqlite3"
    monkeypatch.setenv("MLA_DB", str(db))
    assert _sqlite_api_key() == ""
    assert not db.exists()
