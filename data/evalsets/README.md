# 评测集

本目录存放随仓库分发的全部评测数据：4 个 jsonl 文件、5 份合同文本、1 份示例事实。目录是扁平结构，脚本按文件名直接读取。

## 文件一览

| 文件 | 数量 | 用途 |
| --- | --- | --- |
| `eval_cases.jsonl` | 52 条 | 主评测集：规则判定的准确率与召回率、补问触发与不误问、越界拒答与误拒 |
| `eval_generalization.jsonl` | 12 条 | 泛化集：非模板写法，考察抽取在自然表述下是否仍成立 |
| `qa_cases.jsonl` | 20 条 | 问答集：检索与引用准确性 |
| `clause_gold.jsonl` | 95 条 | 条款级 gold：存在性域的覆盖率、A 级误报与引用准确率 |
| `contract_traps_01/02/03.txt` | 3 份 | 对抗合同，人工植入违法条款与缺失条款 |
| `contract_compliant_01.txt` | 1 份 | 全合规对照合同 |
| `sample_contract.txt` | 1 份 | 演示用短文，供 `python -m mla check` 直接运行 |
| `sample_facts.json` | 1 份 | 演示用结构化事实，字段名与抽取器输出一致 |

## 主评测集 eval_cases.jsonl

52 条按 `kind` 分六类：`violation` 20 条、`compliant` 8 条、`deficient` 6 条、`complete` 6 条、`boundary_out` 6 条、`boundary_in` 6 条。前四类走合同核对流程，后两类走问答流程。

字段含义：

- `case_id`：用例编号，评测输出与 gold 对账都按它定位。
- `kind`：类别，取值见上。
- `contract`：合同片段正文（前四类），脚本直接送进核对流程。
- `facts`：人工标注的结构化事实，作为核对输入。为空对象表示该用例刻意不给事实，现场抽取后应触发补问。
- `gold_items`：应报出的规则 id 列表。空列表表示正确结果是没有任何规则命中，属负样本。
- `expect_ask`：是否应触发补问。`deficient` 类均为 true，其余为 false。
- `question` / `mode` / `expect_refuse`：仅越界与界内边缘用例使用。`question` 为问题原文，`mode` 固定为 `qa`，`expect_refuse` 表示是否应拒答。

## 泛化集 eval_generalization.jsonl

12 条，字段与主集一致，另加 `notes` 记录标注理由。`violation` 11 条、`compliant` 1 条。`contract` 是自然语言描述而非模板条文，用来检验抽取脱离模板句式后的表现。

## 问答集 qa_cases.jsonl

20 条，字段为 `case_id`、`kind`（固定 `qa`）、`question`、`gold_statutes`。`gold_statutes` 是人工标注的应引法条 id，评测脚本据此计算引用命中率。

## 条款 gold clause_gold.jsonl

95 条，全部挂在 4 份合同上：3 份对抗合同加 1 份全合规对照。字段为 `gold_id`、`contract`（合同文件名）、`clause`（条款号）、`span`（原文片段）、`grade`、`statute_ids`、`need_facts`、`note`。

标注口径：

- 正样本：应报出的条款，共 38 条。`grade` 为 A 的 33 条、B 的 3 条、C 的 2 条，`statute_ids` 给出应引法条。
- 负样本：合规条款，共 57 条，`grade` 写作 `合规`，`statute_ids` 为空。系统对其报出 A 级结论即记为误报。

## 运行

```bash
python scripts/run_eval.py --extract regex     # 主评测集，纯正则抽取档
python scripts/run_eval.py --cases data/evalsets/eval_generalization.jsonl
python scripts/eval_qa.py                      # 问答集
python scripts/eval_clauses.py --runs 1        # 条款 gold
```

指标定义与当前结果见 [docs/evaluation.md](../../docs/evaluation.md)。

## 局限

- 四套数据全部由本项目开发者自行构造，没有外部盲测集。
- gold 由规则推导加人工标注产生，只有一名标注者，没有第二标注者交叉复核，标注偏差无法估计。
- `clause_gold.jsonl` 的陷阱是人工植入的，比真实合同中的风险更集中、更显眼。
- 因此本目录数据上的指标只描述这批样本上的表现，不能当作泛化结论引用。
