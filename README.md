# NasMove

## 项目目标

NasMove 是一个面向 NAS 的可靠文件传输桌面应用。它以“先复制、再校验、再原子提交、最后按条件删除源文件”为核心流程，目标是在网络中断、应用退出、目标同名等场景下保留可恢复的任务状态，而不是把文件复制视为一次不可追踪的简单操作。

当前代码基于 Python 3.12、PySide6、SQLite、`smbprotocol` 和系统 Keychain／Keyring 设计。Windows 阶段以不连接真实 NAS 的 UI 与应用功能闭环为目标；真实 Synology 验证必须在专用测试共享和真实环境中单独执行。

## 非目标

当前 Windows 阶段不连接真实 NAS，不执行 Synology 故障注入，也不把 Fake SMB 演示结果当作真实断点恢复验证。

本项目当前不包含以下承诺：

- 不承诺离线演示能够传输真实文件或验证真实 SMB 权限。
- 不承诺 Windows 下完整测试套件全部通过；其中部分测试专门依赖 macOS／POSIX 的文件锁、权限、ACL 与目录文件描述符能力。
- 不承诺尚未执行的 NAS 重启、断网、大文件、大量小文件、配额和权限等故障注入场景已经通过。

## 当前开发状态

核心传输与恢复模块已经建立，Windows 离线 UI 和功能接线已完成。真实 Synology 故障注入、生产 SMB 会话接线与 macOS 发布验证暂缓，续作边界和恢复命令已归档。

## 当前实现范围

### 可靠传输与恢复模块

- 领域状态机限制任务和文件项的合法状态迁移，避免任意跳过校验或提交阶段。
- 任务规划会校验本地来源、远端目标路径、冲突策略和可用空间，并把任务先保存为预检状态。
- 传输引擎以远端临时文件、分块写入、检查点、重连和重试为基础；校验成功后才执行原子提交。
- 移动任务的源文件删除是独立阶段，只有在远端提交和完整性校验成功后才允许执行；删除失败会以“完成但有警告”结束，源文件仍保留。
- SQLite 保存连接配置元数据、任务、传输项、检查点、尝试记录和事件；敏感密码不写入这些记录。

### Windows 离线 UI 与应用接线

- 四阶段 UI：连接、来源、目标、任务页面支持前后导航。
- `TaskCreationController` 在独立 Qt 线程执行任务规划，避免文件扫描和空间预检阻塞界面。
- 创建任务按 `PREFLIGHT → QUEUED → enqueue` 顺序处理；新任务追加到现有队列，不覆盖已显示任务。
- 移动任务会在进入后台规划前要求用户明确确认；拒绝确认不会创建计划或写入队列。
- 任务队列支持选择、上下重排、暂停、继续和取消；按钮按当前任务状态自动启用或禁用。
- `TaskController` 通过 Qt signal 将后台事件、进度和结果安全回传到 UI 线程。
- `QueueExecutionController` 在单独 Qt 线程中串行消费队列，防止重复启动多个工作线程。
- 复制进度与远端校验进度分别显示；复制达到 100％不代表任务已经完成。
- 可导出脱敏任务摘要，其中不包含技术错误、凭据和本地／NAS 路径。

### 离线演示

`tools/run_windows_ui_demo.py` 使用内存仓储、Fake 连接测试、Fake 网关和 Fake 队列构造完整 UI 闭环。演示任务状态为：

```text
QUEUED → RUNNING → VERIFYING → COMMITTING → COMPLETED
```

它会同步展示复制与校验进度到 100％，但不会发起 SMB 连接、传输真实文件或删除源文件。

## 任务状态与安全边界

任务的主要状态流转如下：

```text
DRAFT → PREFLIGHT → QUEUED → RUNNING → VERIFYING → COMMITTING
                                              └→ DELETING_SOURCE（仅移动任务）
COMMITTING／DELETING_SOURCE → COMPLETED 或 COMPLETED_WITH_WARNINGS

RUNNING／VERIFYING 可进入 PAUSED、INTERRUPTED、WAITING_FOR_NETWORK、FAILED 或 CANCELED
PAUSED → QUEUED 或 RUNNING；INTERRUPTED 可重新预检或重新入队
```

关键约束：

- 只有校验与原子提交成功后，移动任务才可请求删除源文件。
- 队列取消只发送给当前活动任务的协作取消令牌，不会把取消请求遗留给下一个任务。
- UI 只显示安全摘要；底层异常、口令、完整本地路径和 NAS 路径不直接暴露在导出结果中。
- 真实 NAS 测试使用专用共享目录 `NasMoveTest/<run-id>`；认证材料、认证报文和公网地址不得写入测试记录。

## 模块职责

| 目录 | 职责 |
| --- | --- |
| `src/nasmove/core` | 领域模型、状态枚举、状态迁移和重试／删除授权规则。 |
| `src/nasmove/planning` | 来源扫描、路径规则、冲突处理和任务预检。 |
| `src/nasmove/transfer` | 分块传输、检查点、校验、原子提交、删除和队列协调。 |
| `src/nasmove/persistence` | SQLite schema 与任务、传输项、检查点等持久化。 |
| `src/nasmove/smb` | SMB 网关、能力探测和 SMB 错误映射。 |
| `src/nasmove/security` | Keychain／Keyring 凭据存储和日志脱敏。 |
| `src/nasmove/ui` | PySide6 页面、控制器、队列执行控制器和离线演示组合根。 |
| `tests` | 单元测试、集成测试、Fake SMB 与真实 Synology 条件化测试。 |

## Windows 离线运行

### 环境准备

推荐使用项目提供的 Conda 环境。首次创建：

```powershell
conda env create -f environment.windows.yml
conda activate nasmove-py312
python tools/run_windows_ui_demo.py
```

已有环境时更新依赖：

```powershell
conda env update -n nasmove-py312 -f environment.windows.yml --prune
conda activate nasmove-py312
```

离线演示窗口标题包含“离线演示（不连接 NAS）”。它用于验证 UI 和应用接线，不代表真实 NAS 传输已通过。

### 开发与验证

定向验证命令：

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python -m pytest tests/unit/ui tests/unit/transfer/test_queue.py -q
ruff check src tests tools
python -m mypy src/nasmove/ui src/nasmove/transfer/transfer_engine.py
```

2026-09-07 的 Windows 定向验证结果：

- `python -m pytest tests/unit/ui tests/unit/transfer/test_queue.py -q`：37 passed。
- `ruff check src tests tools`：通过。
- `python -m mypy src/nasmove/ui src/nasmove/transfer/transfer_engine.py`：14 个源文件通过。
- `git diff --check`：通过；Windows 工作区可能出现 LF／CRLF 转换提示，不属于内容错误。

Windows 完整测试套件仍包含依赖 POSIX 文件锁、权限和 ACL 的测试，不能直接作为 Windows 通过标准。最近一次完整套件尝试结果已记录在下方续作文档中；不要把其中的失败归因于本轮 UI 改动，也不要将其写为通过。

## 回到真实 NAS 环境后的接续顺序

1. 在 macOS 生产组合根中构造真实 `TaskPlanner`、SQLite 仓储、`ApplicationService`、`TaskCommandService`、`QueueCoordinator` 和 SMB 会话生命周期。
2. 将生产 `ProgressTracker` 的事件出口接入 `TaskController.publish_progress()`，验证大任务的节流和 UI 响应。
3. 使用专用 Synology 测试共享执行能力探测、基础 COPY、断网、NAS 重启、源变化、空间／权限、同名竞争、大文件和大量小文件测试。
4. 回填真实测试矩阵；未执行项必须保持“未执行”，不得用 Fake 演示或局部单测替代。
5. 单独规划 POSIX 测试夹具的跨平台改造，不得为了 Windows 全绿删除 macOS 的安全断言。

## 设计文档

| 文档 | 用途 |
| --- | --- |
| [可靠传输应用设计](docs/superpowers/specs/2026-09-05-nas-reliable-transfer-design.md) | 完整设计、可靠性不变量、状态模型、传输协议和验收标准。 |
| [实施计划](docs/superpowers/plans/2026-09-05-nas-reliable-transfer.md) | 分阶段实施任务和验收安排。 |
| [Windows 离线开发与换机续作记录](docs/verification/windows-offline-development.md) | 当前环境、已完成工作、验证证据和换机后的接续步骤。 |
| [Synology 故障验证矩阵](docs/verification/synology-test-matrix.md) | 真实 NAS 场景的执行次数、结果与待办。 |
