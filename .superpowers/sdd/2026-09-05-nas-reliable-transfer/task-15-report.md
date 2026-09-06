# Task 15 实施报告：连接、来源和目标 UI

## 状态

实施完成，待独立复审。

## 实施摘要

- 新增 PySide6 连接配置页：可编辑 SMB 地址／端口／共享／用户名／域／密码，默认端口 445，默认要求加密，密码保存仅通过注入的 Keychain 接口。
- 支持 `smb://host/share/path` 地址拆分，连接测试在后台 QThread 执行，阶段结果和技术错误码可展开显示。
- 新增来源页：多文件／目录选择、拖放、固定保留目录层级；移动模式强制完整校验。
- 新增目标页：远端路径先经 `normalize_remote_path()`，SMB 目录浏览和可用空间查询在后台线程执行。
- 新增可取消的 `BackgroundCommandWorker`，避免在 UI 线程执行连接或 SMB 浏览。

## RED/GREEN

GLM 留下的 UI 测试在实现前因 `No module named nasmove.ui` 失败。实现后连接页测试通过；并修正测试夹具在 PySide6 6.11 中不存在 `QThread.currentThreadId()` 的兼容问题。

## 验证

```text
QT_QPA_PLATFORM=offscreen python -m pytest tests/unit/ui -q
5 passed

python -m pytest -q
785 passed, 1 skipped

ruff check src tests
All checks passed!

python -m mypy src/nasmove
Success: no issues found in 35 source files
```

真实 SMB 能力探测仍因未设置 `NASMOVE_TEST_SMB=1` 跳过。

## 风险与后续

- 当前页面实现提供任务创建前的配置与浏览能力；任务队列、进度和结果展示属于 Task 16。
- macOS 原生 Keychain 和真实 SMB 的人工验收仍需在专用 NAS 测试环境执行。
