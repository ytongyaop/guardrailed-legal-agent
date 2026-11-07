"""三档消融配置（同回归集与留出集、同标注口径）。"""

from __future__ import annotations

ABLATION_CONFIGS: dict[str, dict[str, bool]] = {
    "llm_only": {"rules": False, "retrieval": False},
    "llm_rules": {"rules": True, "retrieval": False},
    "full": {"rules": True, "retrieval": True},
}
