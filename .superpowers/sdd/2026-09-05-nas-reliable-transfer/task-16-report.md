# Task 16 增量报告：队列、进度和结果 UI

## 状态

Windows 离线 UI 与功能接线已完成；生产 macOS 组合根、真实 SMB 会话和 NAS 验证按计划延后。

## 已完成

- 新增 `TaskPage`，分离复制进度与远端校验进度，显示速度、ETA、状态、暂停／继续／取消按钮。
- 校验阶段即使复制进度为 100%，仍显示“正在校验”，避免误报任务完成。
- 源文件删除失败的 `COMPLETED_WITH_WARNINGS` 结果显示“迁移完成，但源文件仍保留”。
- 新增基础 `MainWindow`，组合连接、来源、目标和任务页面。
- 扩展测试构造器以生成进度快照和源保留结果。
- 新增 `TaskController`，通过 Qt signal 将工作线程事件和进度安全送回 UI 线程。
- 暂停、继续和取消按钮已转发到注入的任务命令端口。
- 任务队列支持显示、选择和上下重排，重排通过仓储接口持久化。
- 主窗口支持四个阶段的上一步／下一步导航。
- 支持导出不包含技术错误、凭据和路径的脱敏任务摘要。
- 新增后台任务创建控制器，将三页输入转换为 `PlanRequest`，调用现有规划器并按 `PREFLIGHT → QUEUED → enqueue` 顺序入队。
- 队列位置从现有任务最大值顺延；创建成功后追加显示且自动切换到任务页。
- 新增 UI 任务命令服务，排队态暂停／取消和暂停态继续均通过领域状态迁移；运行态暂停／取消使用协作 token。
- `QueueCoordinator` 增加活动取消入口；没有活动 token 时取消请求不保留，避免误取消后续任务。
- 新增后台队列执行控制器，单线程消费 `QueueCoordinator.run_next()`，逐项回传结果，异常只进入脱敏 UI 边界。
- 新增 Windows 离线演示组合根和启动脚本，可在无 NAS 时走完整 UI 流程；该结果不计入真实 NAS 验证。
- 移动任务在规划前增加明确确认门槛；用户取消时不创建计划、不入队。
- 暂停、继续和取消按钮按任务状态启用，终态任务不可再次操作。
- 离线演示可完整展示 `QUEUED → RUNNING → VERIFYING → COMMITTING → COMPLETED`，并同步刷新队列状态及复制／校验进度。

## 验证

```text
python -m pytest -q
785 passed, 1 skipped in 70.84s

ruff check src tests
All checks passed!

python -m mypy src/nasmove
Success: no issues found in 37 source files
```

2026-09-07 Windows 定向验证：

```text
python -m pytest tests/unit/ui tests/unit/transfer/test_queue.py -q
37 passed

ruff check src tests tools
All checks passed!

python -m mypy src/nasmove/ui src/nasmove/transfer/transfer_engine.py
Success: no issues found in 14 source files

git diff --check
通过（仅有 Windows 工作区 LF／CRLF 提示）
```

真实 SMB 能力探测按用户要求延后。生产 macOS 组合根与真实 SMB 会话生命周期仍需在对应环境收敛。完整测试套件含大量 POSIX 专用路径、文件锁、权限和 ACL 断言，当前 Windows 运行结果已记录到 `docs/verification/windows-offline-development.md`，不得记为通过。
