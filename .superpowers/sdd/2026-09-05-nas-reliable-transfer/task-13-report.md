# Task 13 实施报告：传输引擎、串行队列和进度模型

## 状态

DONE

## TDD 证据

先新增引擎和队列测试并运行，按预期因缺少模块失败：

```text
ModuleNotFoundError: No module named 'nasmove.transfer.transfer_engine'
```

随后新增进度测试，并以最小实现使 RED 测试进入 GREEN。

## 实施摘要

- 新增 `TransferEngine`，按恢复决策、检查点复制、完整回读校验、原子提交和移动安全删除的顺序处理每个文件。
- 复制、暂停、取消、可重试网络中断、校验失败和一般异常均先尝试持久化安全项目／任务状态，再发布不可变 `TransferEvent`。
- 新增 `TaskResult`，区分成功、失败、警告和最终任务状态；COPY 不调用删除服务，MOVE 只通过 `SourceDeletionService` 删除源文件。
- 新增 `QueueCoordinator`，使用稳定 FIFO 顺序和互斥锁保证同一时刻只有一个引擎调用。
- 新增 `ProgressTracker`／`ProgressSnapshot`，完整校验时总工作量为复制字节加远端回读字节；速度采用最近 30 秒样本，少于 3 个样本时 ETA 为 `None`；UI 事件最多每 250 毫秒、数据库累计最多每秒发布。
- 扩展传输测试夹具，提供引擎／队列用内存端口和任务／项目查询能力；未改动无关的 Task 6、Task 12 报告或缓存。

## 验证

```text
/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/python -m pytest tests/unit/transfer/test_transfer_engine.py tests/unit/transfer/test_progress.py tests/unit/transfer/test_queue.py -q
9 passed
```

```text
/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/python -m pytest tests/unit/transfer -q
58 passed
```

```text
/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/python -m pytest -q
744 passed, 1 skipped
```

```text
/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/ruff check src tests
All checks passed!
```

```text
/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/python -m mypy src
Success: no issues found in 28 source files
```

```text
git diff --check
通过
```

跳过项是需要显式设置 `NASMOVE_TEST_SMB=1` 的真实 SMB 能力探测测试。

## 风险与边界

- 引擎保持同步执行，应用层负责将其放入唯一后台工作线程；队列本身不创建线程。
- 任务仓储现有接口未声明 `list_items(task_id)`，引擎优先使用该显式查询端口，并兼容测试夹具的 `items` 映射；后续持久化接口应补齐项目列表查询。
- 重试策略在本任务中将可重试中断持久化为 `WAITING_RETRY`／`WAITING_FOR_NETWORK`，实际退避等待和会话重连仍由上层恢复协调器负责。

## 提交

提交信息：`feat: orchestrate serial reliable transfers`

## 独立复审修复

复审发现并修复以下问题：

- `TargetCommitter.commit()` 已负责 metadata 持久化和 `VERIFIED → COMMITTED`，引擎不再重复迁移；提交后重新读取项目状态，再继续 MOVE 删除或 COPY 完成。
- `Repository` 正式声明 `list_items(task_id)`；`SqliteTaskRepository` 按 `item_id` 稳定顺序返回项目，引擎移除 fixture-only 映射回退。枚举异常会将任务持久化为 `FAILED` 后发布事件。
- `QueueCoordinator.run_next()` 将取任务、空队列和引擎调用统一置于 `try/finally`，任何异常或连续空队列轮询都会正确释放锁。
- 测试夹具的提交器现在模拟真实提交器的状态 CAS，队列并发统计来自实际执行计数，不再手工伪造最大并发值。

修复前新增回归测试按预期失败（7 failed）；修复后定向测试：

```text
python -m pytest tests/unit/transfer/test_transfer_engine.py tests/unit/transfer/test_queue.py tests/unit/persistence/test_repository.py -q
52 passed
```

本轮修复提交信息：`fix: close transfer orchestration review findings`
