"""测试全局配置：不联网、不花钱、结果确定。

三层保险：
1. 抽取强制走正则（MLA_EXTRACT_MODE=regex）—— 抽取器此时是 RegexExtractor，模型根本不会被构造出来；
2. 屏蔽 .streamlit/secrets.toml 里的真实 Key —— 否则 run_check 的「处理建议」仍会真的请求 DeepSeek；
3. 数据库指向临时目录 —— 命令行会从 settings 表读界面保存的 Key，本机库里有真实 Key，
   不隔离的话 check 相关用例会真的请求 DeepSeek（既慢又花钱）。指向不存在的文件同时保证
   测试不会写坏工作目录里的 mla.sqlite3。
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _offline_llm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """测试统一走正则抽取 + 无 Key：任何测试都不会因为大模型而联网。"""
    monkeypatch.setenv("MLA_EXTRACT_MODE", "regex")
    # load_config() 是调用期读环境变量 / secrets，这里 patch 到 config 层即可整体生效
    monkeypatch.setattr("mla.config._secrets_section", lambda name: {})
    monkeypatch.delenv("MLA_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    # 不存在的库文件：读 Key 兜底直接返回空串，也不会创建数据库
    monkeypatch.setenv("MLA_DB", str(tmp_path / "test-mla.sqlite3"))
