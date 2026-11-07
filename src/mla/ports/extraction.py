"""抽取接口：正则档与模型档共用同一结果结构，可在 `Deps` 里互换（便于两档消融）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ExtractionResult:
    """一次抽取的产物：字段值、证据片段、是否降级、诊断信息。"""

    facts: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)  # 字段 -> 合同原文片段
    degraded: bool = False
    errors: tuple[str, ...] = ()


class Extractor(Protocol):
    def extract(self, text: str) -> ExtractionResult:
        """从合同正文抽取关键事实；抽不到的字段不出现。"""
        ...
