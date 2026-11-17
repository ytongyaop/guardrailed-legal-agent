"""测试全局配置：不联网、不花钱、结果确定。

四层保险：
1. 抽取强制走正则（MLA_EXTRACT_MODE=regex）—— 抽取器此时是 RegexExtractor，模型根本不会被构造出来；
2. 屏蔽 .streamlit/secrets.toml 里的真实 Key —— 这条桩只服务 embedding：`[embedding].api_key`
   会把开发者的真实向量检索 Key 带进来。LLM 一侧已不读 secrets（阶段三起由
   `services.credentials` 统一解析），这里保留它就是为了只防 embedding 真 Key；
3. 运行期目录全部指向临时目录（MLA_HOME / MLA_DB / MLA_INDEX_DIR）—— 命令行会从 settings
   表读界面保存的 Key，本机库里有真实 Key；不隔离的话 check 相关用例会真的请求 DeepSeek。
   MLA_HOME 故意**不预先创建**，用来验证「只读解析不创建目录」；再把 cwd 切到同一个空目录，
   隔离旧版「按启动目录找数据库 / 缓存」的行为，不写坏工作目录里的 mla.sqlite3；
4. 显式放行无模型路径（MLA_ALLOW_NO_LLM=1）—— 服务层门禁默认拒绝无模型核对（规格 003），
   而这些用例测的正是"无模型时的规则 / 缺项 / 落库行为"；门禁自身由 test_no_model_gate.py
   显式关掉开关来验证。
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _offline_llm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """测试统一走正则抽取 + 无 Key：任何测试都不会因为大模型而联网。"""
    monkeypatch.setenv("MLA_EXTRACT_MODE", "regex")
    # 服务层门禁用例见 test_no_model_gate.py：这里显式放行，保证现有无模型用例继续走在旧路径上
    monkeypatch.setenv("MLA_ALLOW_NO_LLM", "1")
    # load_config() 是调用期读环境变量 / secrets，这里 patch 到 config 层即可整体生效
    monkeypatch.setattr("mla.config._secrets_section", lambda name: {})
    monkeypatch.delenv("MLA_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    # 不存在的库文件：读 Key 兜底直接返回空串，也不会创建数据库
    monkeypatch.setenv("MLA_DB", str(tmp_path / "test-mla.sqlite3"))
    # 用户目录：本用例专属，且不预先创建（只读解析不得建目录）
    monkeypatch.setenv("MLA_HOME", str(tmp_path / "mla-home"))
    # 向量缓存目录：不落到工作目录
    monkeypatch.setenv("MLA_INDEX_DIR", str(tmp_path / "index"))
    # 旧版按 cwd 发现数据库 / 缓存；切到空临时目录，避免读到开发者真实数据
    monkeypatch.chdir(tmp_path)
