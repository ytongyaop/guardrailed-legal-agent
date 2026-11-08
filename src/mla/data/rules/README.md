# 确定性规则表（rules.yaml）

一条规则 = 一个可判定条件 + 一个法条 ID：

```yaml
- rule_id: R-PROBATION-1
  name: 三年以上固定期限合同试用期不得超过六个月
  field: probation_months
  op: "<="
  threshold: 6
  statute_id: LCL-19
  severity: 硬性
```

覆盖首期 20–40 条：试用期上限/试用期工资、加班费倍率、竞业期限与补偿、未签书面合同、违约金情形、社保缴纳、工时制度。
