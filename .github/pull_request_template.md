## 改了什么

<!-- 列出本次改动的文件与关键行为变化 -->

## 为什么改

<!-- 说明动机：修哪个问题、支持哪个场景，关联的 issue 编号 -->

## 怎么验证

<!-- 贴出实际执行的命令与输出，不要只写"已测试" -->

```bash
pytest
ruff check src tests scripts
python scripts/check_ui_text.py
```

## 检查清单

- [ ] 已运行 `pytest`
- [ ] 已运行 `ruff check src tests scripts`
- [ ] 已运行 `python scripts/check_ui_text.py`
- [ ] 若改了判定逻辑，已跑回归集确认不退化
- [ ] 若新增评测样本，已按 `data/evalsets/README.md` 的纪律归入回归集或留出集
