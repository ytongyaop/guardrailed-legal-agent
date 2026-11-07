"""校验器：有结论必有依据；建议同样必须有依据；案例不得当结论依据。

约束：依据可追溯率必须为 100%，建议无依据率必须为 0。
纯函数，不依赖外部，便于单测与复用（核对与问答共用）。
"""

from __future__ import annotations

from .models import CheckItem, SourceKind, Suggestion, VerifyReport, source_kind


def verify(
    items: list[CheckItem],
    suggestions: list[Suggestion],
    allowed_source_ids: set[str],
) -> VerifyReport:
    """逐条检查依据。

    规则：
    1. 每个 CheckItem 必须有 `source_id`，且属于 `allowed_source_ids`；
    2. `source_id` 若是案例（C-），不得作为结论依据；
    3. 每条 Suggestion 至少要有一个 `source_id`，且全部属于允许集合；
    4. 违规条目汇总为 errors；errors 非空 → ok=False（由编排层决定降级）。
    """
    errors: list[str] = []

    for item in items:
        sid = (item.source_id or "").strip()
        if not sid:
            errors.append(f"{item.item_id}: 结论缺少依据（source_id 为空）")
            continue
        if sid not in allowed_source_ids:
            errors.append(f"{item.item_id}: 依据不在本轮可用集合内（{sid}）")
            continue
        if source_kind(sid) is SourceKind.CASE:
            errors.append(f"{item.item_id}: 案例不得作为结论依据（{sid}）")

    for idx, sug in enumerate(suggestions, start=1):
        ids = [s.strip() for s in sug.source_ids if s and s.strip()]
        if not ids:
            errors.append(f"建议#{idx}: 未绑定任何依据")
            continue
        unknown = [s for s in ids if s not in allowed_source_ids]
        if unknown:
            errors.append(f"建议#{idx}: 依据不在可用集合内 {unknown}")

    return VerifyReport(ok=not errors, errors=tuple(errors), degraded=bool(errors))
