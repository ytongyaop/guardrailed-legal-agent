"""合同名清洗测试：评测集文件名不是用户的合同名，不得出现在界面与存档里。

函数必须幂等——历史数据里已经存着清洗值，再清洗一遍不能变样。
"""

from __future__ import annotations

import pytest

from mla.adapters.files import normalize_contract_name


@pytest.mark.parametrize(
    "raw",
    ["contract_traps_01.txt", "contract_compliant_01.txt", "sample_contract", "sample_contract.txt"],
)
def test_eval_filenames_become_sample(raw: str) -> None:
    assert normalize_contract_name(raw) == "示例合同"


@pytest.mark.parametrize("raw", ["CONTRACT_TRAPS_07.TXT", "contract_traps_12"])
def test_any_trap_index_and_case_is_masked(raw: str) -> None:
    assert normalize_contract_name(raw) == "示例合同"


def test_real_filename_keeps_stem() -> None:
    assert normalize_contract_name("张三-劳动合同.docx") == "张三-劳动合同"


@pytest.mark.parametrize("raw", ["", "   ", "\t"])
def test_blank_name(raw: str) -> None:
    assert normalize_contract_name(raw) == "未命名合同"


@pytest.mark.parametrize("raw", ["示例合同", "张三-劳动合同", "未命名合同"])
def test_idempotent(raw: str) -> None:
    assert normalize_contract_name(raw) == raw


@pytest.mark.parametrize("raw", ["dir/contract_traps_02.txt", "C:\\tmp\\contract_traps_02.txt"])
def test_path_is_stripped(raw: str) -> None:
    """带目录的路径（含 Windows 反斜杠）都要先取文件名再清洗。"""
    assert normalize_contract_name(raw) == "示例合同"
