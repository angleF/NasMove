# Task 16 增量报告：队列、进度和结果 UI

## 状态

基础页面已实现，完整队列接线与独立复审仍待后续收敛。

## 已完成

- 新增 `TaskPage`，分离复制进度与远端校验进度，显示速度、ETA、状态、暂停／继续／取消按钮。
- 校验阶段即使复制进度为 100%，仍显示“正在校验”，避免误报任务完成。
- 源文件删除失败的 `COMPLETED_WITH_WARNINGS` 结果显示“迁移完成，但源文件仍保留”。
- 新增基础 `MainWindow`，组合连接、来源、目标和任务页面。
- 扩展测试构造器以生成进度快照和源保留结果。

## 验证

```text
python -m pytest -q
785 passed, 1 skipped in 70.84s

ruff check src tests
All checks passed!

python -m mypy src/nasmove
Success: no issues found in 37 source files
```

真实 SMB 能力探测仍因未设置 `NASMOVE_TEST_SMB=1` 跳过；队列重排、事件接线和脱敏导出仍需继续实现。
