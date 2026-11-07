# 案例素材库（cases.jsonl）

只收**官方发布**的典型案例（最高法典型案例 / 人社部与最高法联合发布 / 指导性案例），**不爬裁判文书网**。
一行一条，对应 `mla.domain.models.CaseEntry`：`case_id`、`title`、`source`、`batch`、`publish_date`、
`dispute_type`、`holding`（裁判要旨）、`statute_ids`、`region`、`level`。

用途红线：**只作类案参照**；有法条依据时必须优先法条；界面须标"不构成本案结论"。
