"""版本号单一真源守卫。

版本号只在 `src/mla/__init__.py` 的 `__version__` 定义，`pyproject.toml` 通过
`[tool.setuptools.dynamic] version = {attr = "mla.__version__"}` 引用这一处，
避免两处各写一份而逐步漂移。
"""

from __future__ import annotations

import re
from importlib import metadata

import pytest

import mla


def test_version_format() -> None:
    """版本号必须是三段式数字，便于语义化版本比较。"""
    assert re.fullmatch(r"\d+\.\d+\.\d+", mla.__version__), mla.__version__


def test_installed_metadata_matches_package() -> None:
    """已安装的分发包元数据应与包内版本号一致。

    未安装时（例如只在源码目录直接跑测试）`importlib.metadata` 取不到分发包，
    此时跳过而不是失败，避免把环境差异当成版本漂移。
    """
    try:
        installed = metadata.version("mla")
    except metadata.PackageNotFoundError:
        pytest.skip("mla 未安装，跳过已安装元数据一致性检查")
    assert installed == mla.__version__
