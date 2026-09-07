# Windows 离线开发与换机续作记录

## 当前开发边界

Windows 阶段只实现和验证不依赖真实 NAS 的 UI、领域逻辑、队列编排及 Fake SMB 流程。真实 Synology 故障注入、macOS Keychain、睡眠／唤醒、签名、公证和 universal app 打包延后到具备真实环境后执行。

模拟测试不得写入 `docs/verification/synology-test-matrix.md` 的“通过”列，也不得用于宣称真实 NAS 断点恢复已经通过。

## 环境恢复

在仓库根目录执行：

```powershell
conda env create -f environment.windows.yml
conda activate nasmove-py312
$env:QT_QPA_PLATFORM = "offscreen"
python -m pytest tests/unit/ui -q
ruff check src tests
python -m mypy src/nasmove/ui
```

如果环境已经存在：

```powershell
conda env update -n nasmove-py312 -f environment.windows.yml --prune
```

启动 Windows 离线演示：

```powershell
conda activate nasmove-py312
python tools/run_windows_ui_demo.py
```

窗口标题固定包含“离线演示（不连接 NAS）”。该入口只验证 UI 与应用接线，不传输数据、不连接 NAS，也不删除用户选择的源文件。

## 2026-09-07 已完成

- 建立 `nasmove-py312` Conda 环境，实际 Python 版本为 3.12.14。
- 为 `MainWindow` 增加连接、来源、目标和任务页面的上一步／下一步导航。
- 新增 `TaskController`，将任意工作线程发布的传输事件和进度通过 Qt signal 转交 UI 线程。
- 将暂停、继续和取消按钮绑定到注入的任务命令端口；按钮随任务状态启用，未选择任务或任务已终结时不执行命令。
- 增加任务队列展示与上下重排，并通过仓储的 `reorder_queued_tasks()` 持久化完整排列。
- 分离显示复制和校验进度；复制达到 100％后仍可显示正在校验。
- 增加脱敏摘要导出，只输出用户可见状态、结果和进度，不输出技术错误、凭据或本地／NAS 路径。
- 新增 `TaskCreationController`，把连接配置、来源、目标路径和复制／移动选项组装成 `PlanRequest`。
- 任务规划在独立 `QThread` 中运行，避免文件扫描和空间预检阻塞 UI。
- 新任务的 `queue_position` 追加到现有最大位置之后；规划完成后按 `PREFLIGHT → QUEUED → application.enqueue()` 顺序进入队列。
- 任务创建成功后切换到任务页并追加到现有队列，不覆盖已经显示的任务。
- 输入无效或后台规划失败时只显示安全摘要，不把原始异常、路径或凭据直接写入 UI。
- 新增 `TaskCommandService`：排队任务暂停／取消直接持久化；暂停任务继续时先转回 `QUEUED` 再重新入队；运行中任务使用协作式 token。
- `QueueCoordinator` 新增 `request_cancel()`，取消请求只送达当前活动 `CancellationToken`；没有活动任务时不会误取消后续任务。
- 新增 `QueueExecutionController`，在单独 Qt 线程中持续消费串行队列，将每个 `TaskResult` 安全送回任务页；重复启动不会创建第二个 worker。
- 新增 `tools/run_windows_ui_demo.py` 离线演示入口，使用内存仓储、Fake 连接测试与 Fake 队列完成 UI 闭环，并明确禁止把结果当作真实 NAS 验证。
- 移动任务在进入后台规划前要求用户明确确认；拒绝确认不会创建计划或写入队列。
- 离线演示覆盖 `QUEUED → RUNNING → VERIFYING → COMMITTING → COMPLETED`，复制与校验进度均到达 100％，队列文本同步更新为完成状态。

## 本轮验证证据

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

完整测试套件目前不是 Windows 可移植套件：测试夹具大量使用 `/source/...` macOS／POSIX 路径，且文件锁、目录文件描述符、权限和 ACL 测试依赖 `fcntl`、`os.getuid()`、`dir_fd` 等 POSIX 能力。本轮完整套件尝试结果为 `603 passed, 10 skipped, 107 failed, 63 errors`；这不是本次 UI 改动引入的单一回归，不能掩盖为通过。

## 下一步

1. 在 macOS 应用组合根中构造真实 `TaskPlanner`、仓储、`ApplicationService`、`TaskCommandService` 和 `QueueCoordinator`，并接入真实 SMB 会话生命周期。
2. 把生产 `ProgressTracker` 的事件出口注入 `TaskController.publish_progress()`，验证大任务下的节流和 UI 响应。
3. 单独规划 POSIX 测试夹具的跨平台改造；不得为了 Windows 绿灯删除 macOS 安全断言。
4. 回到真实环境后继续执行 `synology-test-matrix.md` 中仍为“未执行”的场景。
