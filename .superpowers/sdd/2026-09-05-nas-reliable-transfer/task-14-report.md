# Task 14 实施报告：应用启动恢复与生命周期

## 状态

DONE

## TDD 证据

先新增 `tests/unit/test_app_recovery.py` 与应用夹具，再运行定向测试；在实现缺失阶段按预期得到 `ModuleNotFoundError: No module named 'nasmove.app'`（RED）。

随后以最小实现新增 `ApplicationService`、`SingleInstanceLock`、启动／关闭报告，并为 `RecoveryCoordinator` 补充启动恢复委托接口。

## 实施摘要

- 单实例锁使用 `~/Library/Application Support/NasMove/app.lock`（可注入测试路径），父目录限制为当前用户私有 `0700`，锁文件为当前用户拥有的普通文件 `0600`，通过非阻塞 `flock` 报告已有实例。
- 启动顺序固定为完整性检查、标记遗留活动任务为 `INTERRUPTED`、枚举未完成任务、按持久化队列顺序入队；Keychain 缺失或读取异常时 fail-closed，将可暂停状态置为 `PAUSED`，不把密码放入报告、队列或日志。
- 关闭先停止接收新任务并请求协作暂停，等待安全边界（队列实现若不尊重超时，应用侧仍以有界守护等待返回）；完成后刷新／保存检查点、暂停活动及排队任务，按 SMB、SQLite、单实例锁顺序释放资源。
- 超过等待上限只返回“仍在安全暂停中”，保留 SMB、数据库和锁，不强杀传输工作线程。
- 新增真实 SQLite 重启边界测试，确认活动任务重启后进入 `interrupted`，不依赖单纯 trace 断言。

## 验证

```text
/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/python -m pytest tests/unit/test_app_recovery.py tests/fault/test_forced_quit_recovery.py -q
8 passed
```

```text
/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/python -m pytest -q
758 passed, 1 skipped in 59.58s
```

```text
/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/ruff check src tests
All checks passed!
```

```text
/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/python -m mypy src
Success: no issues found in 29 source files
```

```text
git diff --check
通过
```

跳过项为需要显式设置 `NASMOVE_TEST_SMB=1` 的专用真实 SMB 能力探测测试。

## 风险与边界

- 应用服务不创建额外后台传输线程；`QueueCoordinator` 保持同步编排，生命周期服务通过其可选安全边界／刷新接口与现有工作线程协作。
- 强退窗口的安全性依赖既有检查点、完整校验、原子重命名和删除授权状态机；本任务只负责重启时的持久化状态边界，不自动删除任何来源或远端临时文件。
- macOS 真实 Keychain／SMB 会话与 30 秒人工 UI 体验仍需发布阶段在专用环境验证。

## 提交

提交信息：`feat: recover tasks across app restarts`
