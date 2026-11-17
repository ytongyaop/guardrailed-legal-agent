"""`mla auth list/set/remove`：隐藏输入、保存前校验、无 Key 输出（规格 002 第 6 章、AC-09）。

全部走进程内 `main([...])`：conftest 已把运行目录指到本用例的临时目录、屏蔽真实 Key，
本模块再把凭据文件的权限收紧（whoami / icacls）与 `GET /models` 探针一并打桩，不联网。

断言口径：Key 一律用固定哑串（`sk-dummy-` 前缀），只检查"出现过 / 没出现过"，
输出、文件内容与异常里都不允许出现真实 Key。
"""

from __future__ import annotations

import json
import subprocess

import pytest

from mla.adapters import auth_file
from mla.adapters.llm import DeepSeekLLM, ProbeResult
from mla.cli import AUTH_PROBE_MESSAGES, main
from mla.config import mla_home

DUMMY_KEY = "sk-dummy-" + "0" * 26
KEY_PREFIX = "sk-dummy-"


class ProbeStub:
    """`GET /models` 探针替身：记录每次传入的 Key，可随时改结果分类。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.result = ProbeResult(kind="ok", status_code=200, message="HTTP 200")

    def fail(self, kind: str, status_code: int | None = None) -> None:
        self.result = ProbeResult(kind=kind, status_code=status_code, message="")


@pytest.fixture(autouse=True)
def fake_acl(monkeypatch: pytest.MonkeyPatch) -> None:
    """凭据文件权限收紧打桩：调真实 whoami / icacls 会让用例依赖本机 ACL 状态。"""

    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[0] == "whoami":
            return subprocess.CompletedProcess(args, 0, "TESTPC\\tester\n", "")
        if len(args) == 2 and args[0] == "icacls":
            return subprocess.CompletedProcess(args, 0, f"{args[1]} TESTPC\\tester:(F)\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(auth_file, "_run_cmd", fake_run)


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> ProbeStub:
    """校验探针打桩：默认 200（通过），用例可改成 401 / 网络失败。"""
    stub = ProbeStub()

    def fake_probe(self_llm: DeepSeekLLM, key: str, *, timeout_s: float | None = None) -> ProbeResult:
        stub.calls.append((type(self_llm).__name__, key))
        return stub.result

    monkeypatch.setattr(DeepSeekLLM, "probe_models", fake_probe)
    return stub


@pytest.fixture
def typed_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """隐藏输入打桩：`getpass` 不读真实终端。"""
    monkeypatch.setattr("getpass.getpass", lambda prompt="": DUMMY_KEY)


def _auth_path():
    return mla_home() / "auth.json"


def _entry() -> dict | None:
    path = _auth_path()
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8")).get("deepseek")
    return data if isinstance(data, dict) else None


def test_auth_set_saves_key_after_probe_ok(
    probe: ProbeStub, typed_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """校验通过才落盘：文件里有合法条目、来源标签正确、输出里没有哑 Key。"""
    assert main(["auth", "set"]) == 0
    captured = capsys.readouterr()
    assert "已保存到用户凭据文件（连接校验通过）" in captured.out
    assert probe.calls and probe.calls[0][1] == DUMMY_KEY  # 校验的是本次输入的新 Key

    assert _entry() == {"type": "api_key", "key": DUMMY_KEY}
    status = json.loads((mla_home() / "auth-status.json").read_text(encoding="utf-8"))
    assert status["deepseek"]["ok"] is True and status["deepseek"]["kind"] == "ok"

    assert main(["auth", "list"]) == 0
    listing = capsys.readouterr()
    assert "用户凭据文件（auth.json）" in listing.out
    assert str(_auth_path()) in listing.out
    assert KEY_PREFIX not in listing.out + listing.err


def test_auth_set_rejects_invalid_key_without_writing(
    probe: ProbeStub, typed_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """HTTP 401：打印固定文案、退出码 1、不生成凭据文件，也不出现“已保存”。"""
    probe.fail("auth", 401)

    assert main(["auth", "set"]) == 1
    captured = capsys.readouterr()
    assert AUTH_PROBE_MESSAGES["auth"] in captured.err
    assert "已保存" not in captured.out + captured.err
    assert not _auth_path().exists()
    assert KEY_PREFIX not in captured.out + captured.err


def test_auth_set_rejects_network_failure_without_writing(
    probe: ProbeStub, typed_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """断网：同样不落盘，提示必须指向网络而不是“Key 不正确”。"""
    probe.fail("network")

    assert main(["auth", "set"]) == 1
    captured = capsys.readouterr()
    assert AUTH_PROBE_MESSAGES["network"] in captured.err
    assert AUTH_PROBE_MESSAGES["auth"] not in captured.err
    assert not _auth_path().exists()


def test_auth_set_empty_input_exits_1_without_probe(
    probe: ProbeStub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """空输入（含只有空白）直接退出码 1：不打校验、不写文件。"""
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "   ")

    assert main(["auth", "set"]) == 1
    assert probe.calls == []
    assert not _auth_path().exists()
    assert "未输入 Key" in capsys.readouterr().err


def test_auth_list_reports_unconfigured(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """没有任何来源时给出固定提示，退出码 0（列表本身不是错误）。"""
    monkeypatch.delenv("MLA_LLM_API_KEY", raising=False)

    assert main(["auth", "list"]) == 0
    assert capsys.readouterr().out.strip() == "未配置任何服务商凭据。"


def test_auth_list_shows_provider_metadata_without_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """已配置：只显示 provider / 类型 / 来源 / 路径，不含 Key、不含长度。"""
    monkeypatch.delenv("MLA_LLM_API_KEY", raising=False)
    home = mla_home()
    home.mkdir(parents=True, exist_ok=True)
    _auth_path().write_text(json.dumps({"deepseek": {"type": "api_key", "key": DUMMY_KEY}}), encoding="utf-8")

    assert main(["auth", "list"]) == 0
    captured = capsys.readouterr()
    assert "deepseek" in captured.out
    assert "api_key" in captured.out
    assert "用户凭据文件（auth.json）" in captured.out
    assert str(_auth_path()) in captured.out
    assert KEY_PREFIX not in captured.out + captured.err
    assert "长度" not in captured.out + captured.err


def test_auth_remove_deletes_entry_and_is_idempotent(
    probe: ProbeStub, typed_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """remove 删掉条目；重复 remove 幂等（提示“未配置该服务商凭据”，退出码 0）。"""
    assert main(["auth", "set"]) == 0
    captured = capsys.readouterr()
    assert KEY_PREFIX not in captured.out + captured.err

    assert main(["auth", "remove"]) == 0
    assert "已清除" in capsys.readouterr().out
    assert _entry() is None

    assert main(["auth", "remove"]) == 0
    assert "未配置该服务商凭据。" in capsys.readouterr().out


def test_auth_set_takes_no_key_argument(capsys: pytest.CaptureFixture[str]) -> None:
    """`auth set` 不提供任何接收 Key 的命令行参数：Key 只能从隐藏输入读。"""
    with pytest.raises(SystemExit) as excinfo:
        main(["auth", "set", "--help"])
    assert excinfo.value.code == 0
    helped = capsys.readouterr()
    assert "--key" not in helped.out + helped.err

    # 结构断言：真把 Key 当参数传，argparse 必须拒绝，而不是悄悄接收
    with pytest.raises(SystemExit) as rejected:
        main(["auth", "set", "--key", DUMMY_KEY])
    assert rejected.value.code not in (0, None)
    assert "unrecognized arguments" in capsys.readouterr().err


def test_auth_help_lists_three_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    """`mla auth --help` 列出 list / set / remove（CLI 管理闭环可见）。"""
    with pytest.raises(SystemExit) as excinfo:
        main(["auth", "--help"])
    assert excinfo.value.code == 0
    assert "{list,set,remove}" in capsys.readouterr().out


def test_auth_list_reports_corrupt_file_instead_of_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """凭据文件损坏：给出可操作的修复提示（AC-06），不当成“未配置”、不吐堆栈、不带 Key。"""
    monkeypatch.delenv("MLA_LLM_API_KEY", raising=False)
    home = mla_home()
    home.mkdir(parents=True, exist_ok=True)
    _auth_path().write_text("{not json", encoding="utf-8")

    assert main(["auth", "list"]) == 1
    captured = capsys.readouterr()
    assert "无法读取凭据文件" in captured.err
    assert "未配置任何服务商凭据。" not in captured.out
    assert KEY_PREFIX not in captured.out + captured.err
    # 原文件不被覆盖：损坏时只报错，不自动重建
    assert _auth_path().read_text(encoding="utf-8") == "{not json"
