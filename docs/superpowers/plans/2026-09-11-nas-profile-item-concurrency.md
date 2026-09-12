# NAS Profile Item Concurrency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 NAS 连接配置提供每任务 `1`～`4` 个文件工作槽，并在不并行运行多个任务的前提下保持复制、校验、提交、软删除和恢复语义。

**Architecture:** 队列协调器继续只运行一个任务。`TransferEngine` 成为任务级调度器，按任务快照创建独立的项目工作器；每个工作器拥有一套独立 `SmbProtocolGateway`、`SessionInfo` 与 SQLite 线程连接。工作器只返回项目结果，调度器集中决定是否停止派发、等待全部活动工作器退出，以及如何写入任务终态。

**Tech Stack:** Python 3、PySide6、SQLite、smbprotocol、`concurrent.futures.ThreadPoolExecutor`、pytest、ruff、mypy。

**Spec:** `docs/superpowers/specs/2026-09-11-nas-profile-item-concurrency-design.md`

## Global Constraints

- `ConnectionConfig.max_parallel_items` 的默认值固定为 `2`，合法整数范围固定为 `1`～`4`。
- 队列继续串行执行任务；不支持多任务并行，也不实现单文件分片并行。
- 密码只能从 macOS Keychain 读取；不得写入配置、任务快照、SQLite、事件或日志。
- 一个项目必须完整完成恢复、复制、完整回读校验、提交后，移动任务才能将本地源移入废纸篓。
- 每个并发项目必须独占 SMB gateway／session 与 SQLite 线程连接；不得共享现有 session、文件句柄或 connection。
- 空目录项目按原计划顺序串行处理。
- 当前工作树已有无关改动；每个任务结束只检查本任务文件的 diff，除非工作树被清理，否则不创建提交。

---

## File Structure

| 文件 | 责任 |
| --- | --- |
| `src/nasmove/core/model.py` | 连接配置的并发度领域校验。 |
| `src/nasmove/persistence/schema.py` | schema 5 到 6 的受指纹保护升级，以及新库建表。 |
| `src/nasmove/persistence/sqlite_repository.py` | 配置及任务连接快照的读写映射。 |
| `src/nasmove/ui/connection_page.py` | 配置页“传输性能”整数控件及其加载／清空行为。 |
| `src/nasmove/transfer/item_worker.py` | 单项目完整生命周期、结果分类与资源释放。 |
| `src/nasmove/transfer/transfer_engine.py` | 任务内项目调度、停止派发、最终状态聚合。 |
| `src/nasmove/transfer/retrying_runner.py` | 将任务与 Keychain 密码交给引擎工厂；网络重试重建工作器。 |
| `src/nasmove/transfer/progress.py` | 对并发复制／校验回调的锁保护。 |
| `src/nasmove/ui/desktop_app.py` | 生产工作器工厂：每槽新建 gateway、连接、服务和清理逻辑。 |
| `tests/unit/**` | 领域、迁移、UI、调度、资源隔离、停止和回归验证。 |

### Task 1：领域模型与持久化快照

**Files:**

- Modify: `src/nasmove/core/model.py:66-92`
- Modify: `src/nasmove/persistence/schema.py:12-340`
- Modify: `src/nasmove/persistence/sqlite_repository.py:457-530, 828-845`
- Modify: `tests/fixtures/builders.py:47-58`
- Test: `tests/unit/core/test_model.py`
- Test: `tests/unit/persistence/test_schema.py`
- Test: `tests/unit/persistence/test_repository.py`

**Interfaces:**

- Produces: `ConnectionConfig` 的 `max_parallel_items: int = 2` 字段。
- Produces: schema version `6`，其中 `connection_profiles.max_parallel_items` 与 `tasks.connection_max_parallel_items` 均为不可空、默认 `2`、范围 `1`～`4` 的整数列。
- Produces: `_connection_profile_from_row(row)` 和 `_task_from_row(row)` 都从各自表列构建 `max_parallel_items`；`_insert_task(task)` 写入任务快照。

- [ ] **Step 1：写出领域边界的失败测试。**

```python
@pytest.mark.parametrize("value", (1, 4))
def test_connection_config_accepts_parallel_item_bounds(value: int) -> None:
    assert build_connection_config(max_parallel_items=value).max_parallel_items == value


@pytest.mark.parametrize("value", (0, 5, True, "2"))
def test_connection_config_rejects_invalid_parallel_item_count(value: object) -> None:
    with pytest.raises(DomainValidationError, match="max_parallel_items"):
        build_connection_config(max_parallel_items=value)  # type: ignore[arg-type]
```

- [ ] **Step 2：运行领域测试并确认尚未支持该参数。**

Run: `.venv/bin/pytest tests/unit/core/test_model.py -q`

Expected: FAIL，`ConnectionConfig` 尚未接受 `max_parallel_items` 或没有拒绝越界值。

- [ ] **Step 3：以最小领域改动实现默认值和类型／范围校验。**

```python
max_parallel_items: int = 2

if type(self.max_parallel_items) is not int or not 1 <= self.max_parallel_items <= 4:
    raise DomainValidationError("max_parallel_items must be an integer between 1 and 4")
```

- [ ] **Step 4：为 schema 5 升级和快照隔离写失败测试。**

```python
def test_schema_v5_migrates_parallel_item_defaults(tmp_path) -> None:
    connection = create_exact_v5_database(tmp_path / "schema-v5.db")
    initialize_database(connection)
    assert version(connection) == "6"
    assert connection.execute(
        "SELECT max_parallel_items FROM connection_profiles"
    ).fetchone()[0] == 2
    assert connection.execute(
        "SELECT connection_max_parallel_items FROM tasks"
    ).fetchone()[0] == 2


def test_task_uses_parallel_item_snapshot_after_profile_update(repository) -> None:
    profile = build_connection_config(max_parallel_items=4)
    task = build_task_record(connection=profile)
    repository.create_task(task, [build_transfer_item_record()])
    repository.save_connection_profile(replace(profile, max_parallel_items=1))
    assert repository.get_task(task.id).connection.max_parallel_items == 4
```

- [ ] **Step 5：实现受完整指纹校验保护的 schema 6 迁移和 repository 映射。**

```python
SCHEMA_VERSION = "6"
LEGACY_SCHEMA_VERSIONS = ("5", "4", "3")

PARALLEL_ITEMS_SQL = """
ALTER TABLE connection_profiles ADD COLUMN max_parallel_items INTEGER NOT NULL DEFAULT 2
CHECK (max_parallel_items BETWEEN 1 AND 4);
ALTER TABLE tasks ADD COLUMN connection_max_parallel_items INTEGER NOT NULL DEFAULT 2
CHECK (connection_max_parallel_items BETWEEN 1 AND 4);
"""
```

将 `PARALLEL_ITEMS_SQL` 加入新库的 canonical schema 指纹和建库脚本。`_expected_legacy_schema_fingerprint("5")` 必须生成当前 schema 减去该 SQL 的精确版本；`_migrate_legacy_schema` 必须按 `3 → is_archived`、`3／4 → conflict_strategy`、`3／4／5 → PARALLEL_ITEMS_SQL` 的条件执行，然后重新计算 hash 并写入版本 `6`。在 profile SQL 的 insert／upsert 中写入 `max_parallel_items`，在任务 insert 中写入 `connection_max_parallel_items`，所有 row mapper 从对应列读取该值。

- [ ] **Step 6：运行领域和持久化测试。**

Run: `.venv/bin/pytest tests/unit/core/test_model.py tests/unit/persistence/test_schema.py tests/unit/persistence/test_repository.py -q`

Expected: PASS，且所有 v3、v4、v5 升级以及篡改 identity 拒绝测试都通过。

- [ ] **Step 7：检查任务限定 diff。**

Run: `git diff --check -- src/nasmove/core/model.py src/nasmove/persistence/schema.py src/nasmove/persistence/sqlite_repository.py tests/fixtures/builders.py tests/unit/core/test_model.py tests/unit/persistence/test_schema.py tests/unit/persistence/test_repository.py`

Expected: 无输出。

### Task 2：连接配置页的并发控制

**Files:**

- Modify: `src/nasmove/ui/connection_page.py:33-225`
- Test: `tests/unit/ui/test_connection_page.py`
- Test: `tests/unit/ui/test_theme.py`

**Interfaces:**

- Consumes: `ConnectionConfig.max_parallel_items`。
- Produces: `ConnectionPage.parallel_items_spinbox: QSpinBox`，范围 `1`～`4`、默认 `2`。
- Produces: `_config()`、`load_profile()`、`clear_profile()` 与 `_editable_controls()` 均处理该控件。

- [ ] **Step 1：写出默认值、保存、加载和清空的失败测试。**

```python
def test_connection_page_edits_and_restores_parallel_item_count(qtbot) -> None:
    page = ConnectionPage()
    qtbot.addWidget(page)
    assert page.parallel_items_spinbox.minimum() == 1
    assert page.parallel_items_spinbox.maximum() == 4
    assert page.parallel_items_spinbox.value() == 2
    page.parallel_items_spinbox.setValue(4)
    assert page.connection_config().max_parallel_items == 4
    page.load_profile(build_connection_config(max_parallel_items=1))
    assert page.parallel_items_spinbox.value() == 1
    page.clear_profile()
    assert page.parallel_items_spinbox.value() == 2
```

- [ ] **Step 2：运行 UI 测试并确认控件不存在。**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest tests/unit/ui/test_connection_page.py -q`

Expected: FAIL，`parallel_items_spinbox` 尚未定义。

- [ ] **Step 3：添加“传输性能”区块及数据绑定。**

```python
self.parallel_items_spinbox = QSpinBox()
self.parallel_items_spinbox.setRange(1, 4)
self.parallel_items_spinbox.setValue(2)
self.parallel_items_spinbox.setToolTip(
    "同一迁移任务内同时处理的文件数；任务队列仍顺序执行"
)

performance = QGroupBox("传输性能")
performance_layout = QFormLayout(performance)
performance_layout.addRow("并发文件数", self.parallel_items_spinbox)
```

将 group box 放在现有连接表单与 Keychain 选项之间；把 spinbox 的 `valueChanged` 连接到 `configuration_changed`；在 `_config()` 传入 `max_parallel_items`；加载 profile 时 set value，清空时恢复 `2`，并把它加入信号阻塞与可编辑控件元组。

- [ ] **Step 4：运行控件和主题回归。**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest tests/unit/ui/test_connection_page.py tests/unit/ui/test_theme.py -q`

Expected: PASS，三种主题的 group box、spinbox 与提示文案可读。

- [ ] **Step 5：在两个桌面尺寸进行截图回归。**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest tests/unit/ui/test_connection_page.py::test_expanded_connection_details_do_not_compress_input_text -q`

Expected: PASS，既有 `1100×760` 与 `1600×1000` 布局不挤压输入控件。

### Task 3：线程安全进度与可独占的项目工作器

**Files:**

- Create: `src/nasmove/transfer/item_worker.py`
- Modify: `src/nasmove/transfer/progress.py:41-168`
- Modify: `tests/fixtures/transfer.py`
- Test: `tests/unit/transfer/test_progress.py`
- Test: `tests/unit/transfer/test_item_worker.py`

**Interfaces:**

- Produces: `ItemRunOutcome(item_id, state, warning=None, error=None, retryable=False)`。
- Produces: `ItemWorker.run(item: TransferItemRecord, task: TaskRecord, token: CancellationToken) -> ItemRunOutcome` 和 `ItemWorker.close() -> None` 方法。
- Produces: `ItemWorkerFactory = Callable[[TaskRecord, str], ItemWorker]`。
- Produces: thread-safe `ProgressTracker.record_copy`、`record_verification`、`update` 与 `snapshot`。

- [ ] **Step 1：写并发进度和工作器资源关闭的失败测试。**

```python
def test_progress_tracker_totals_parallel_copy_and_verification() -> None:
    tracker = ProgressTracker(400)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: tracker.record_copy(100), range(4)))
    assert tracker.snapshot().copied_bytes == 400


def test_item_worker_releases_gateway_and_repository_connection_after_item(worker) -> None:
    outcome = worker.run(build_transfer_item_record(), build_task_record(), CancellationToken())
    worker.close()
    assert outcome.error is None
    assert worker.gateway.disconnect_calls == 1
    assert worker.repository.release_calls == 1
```

- [ ] **Step 2：运行测试并确认共享状态尚未受保护、工作器尚不存在。**

Run: `.venv/bin/pytest tests/unit/transfer/test_progress.py tests/unit/transfer/test_item_worker.py -q`

Expected: FAIL，缺少 `item_worker` 模块，且现有 tracker 无锁。

- [ ] **Step 3：在 `ProgressTracker` 的所有可变状态读写处使用一个 `threading.RLock`。**

```python
self._lock = RLock()

def record_copy(self, byte_count: int) -> ProgressSnapshot:
    with self._lock:
        self._add("copied", byte_count)
        return self._record(byte_count)
```

对 `set_total`、`record_verification`、`update` 和 `snapshot` 采用同一把锁；仅在锁内计算 snapshot 和更新节流时间戳，事件回调必须在释放锁后调用，避免 UI 回调重入 tracker。

- [ ] **Step 4：提取单项目生命周期为 `TransferItemWorker`。**

```python
@dataclass(frozen=True, slots=True)
class ItemRunOutcome:
    item_id: TransferItemId
    state: ItemState
    warning: str | None = None
    error: BaseException | None = None
    retryable: bool = False

class TransferItemWorker:
    def run(
        self, item: TransferItemRecord, task: TaskRecord, token: CancellationToken
    ) -> ItemRunOutcome:
        return self._run_lifecycle(item, task, token)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._gateway.disconnect()
        self._repository.release_thread_connection()
```

把现有 `_run_item` 的恢复、复制、完整校验、提交和删除逻辑移入 `TransferItemWorker.run`。它只能转换自己的 item state、发布带 `task_id` 和 `item_id` 的事件、返回 `ItemRunOutcome`；不得调用 `transition_task`。在 `finally` 中依次调用 worker 的 gateway `disconnect()` 和 repository `release_thread_connection()`，并使 `close()` 幂等。取消、暂停、可重试网络错误、校验错误和不可恢复错误必须写入原有 item state，再通过 outcome 交给调度器聚合。

- [ ] **Step 5：运行项目生命周期现有回归和新增工作器测试。**

Run: `.venv/bin/pytest tests/unit/transfer/test_progress.py tests/unit/transfer/test_item_worker.py tests/unit/transfer/test_checkpoint_writer.py tests/unit/transfer/test_verification.py tests/unit/transfer/test_commit.py tests/unit/transfer/test_deletion.py -q`

Expected: PASS，尤其是未通过完整校验的移动项目没有调用本地废纸篓。

### Task 4：任务级并发调度与安全停止语义

**Files:**

- Modify: `src/nasmove/transfer/transfer_engine.py:90-360`
- Test: `tests/unit/transfer/test_transfer_engine.py`
- Test: `tests/unit/transfer/test_queue.py`

**Interfaces:**

- Consumes: `ItemWorkerFactory` 与 `ItemRunOutcome`。
- Produces: `TransferEngine` 的 `item_worker_factory: ItemWorkerFactory | None = None` 与 `password: str | None = None` 构造参数。
- Produces: `_run_parallel_items(task, files, token) -> tuple[int, tuple[str, ...], BaseException | None, bool]`，该元组依次表示完成项目数、警告、首个错误、是否可重试。

- [ ] **Step 1：写并发上限、串行兼容、失败停止派发和空目录串行的失败测试。**

```python
def test_parallel_items_never_exceed_task_snapshot_limit(parallel_engine) -> None:
    task = replace(build_task_record(), connection=build_connection_config(max_parallel_items=2))
    result = parallel_engine.run_task(task.id, CancellationToken())
    assert result.success is True
    assert parallel_engine.max_active_workers == 2


def test_parallel_failure_stops_new_items_and_waits_for_started_workers(parallel_engine) -> None:
    result = parallel_engine.run_task(parallel_engine.failing_task.id, CancellationToken())
    assert result.state is TaskState.FAILED
    assert parallel_engine.started_item_ids == {"item-1", "item-2"}
    assert parallel_engine.closed_item_ids == parallel_engine.started_item_ids


def test_empty_directories_are_not_submitted_to_parallel_pool(parallel_engine) -> None:
    parallel_engine.run_task(parallel_engine.task_with_empty_directory.id, CancellationToken())
    assert parallel_engine.empty_directory_worker_count == 0
```

- [ ] **Step 2：运行调度测试并确认当前 engine 仍逐项调用 `_run_item`。**

Run: `.venv/bin/pytest tests/unit/transfer/test_transfer_engine.py tests/unit/transfer/test_queue.py -q`

Expected: FAIL，尚无 `item_worker_factory` 或活动工作器指标。

- [ ] **Step 3：实现文件项目的有限线程池及停止派发。**

```python
with ThreadPoolExecutor(max_workers=task.connection.max_parallel_items) as executor:
    for item in file_items:
        if stop_scheduling:
            break
        future = executor.submit(self._run_worker, item, task, token)
        active[future] = item.id
        if len(active) == task.connection.max_parallel_items:
            collect_one_completed_future(active)
```

`_run_worker` 每次经 `item_worker_factory(task, password)` 创建一个独立 worker，并在 `finally` 调用 `worker.close()`。第一次得到失败、暂停、取消或可重试网络 outcome 时设置一个共享停止事件，不再提交未开始项目。随后继续收集所有已经提交的 futures；向其传递同一个 cancellation token，使复制／校验在原有检查点退出。future 的异常转为不可恢复 `ItemRunOutcome`，不得遗漏或仅写日志。

- [ ] **Step 4：集中写入任务状态，随后串行运行空目录。**

```python
if canceled:
    return self._finish(task, TaskState.CANCELED, False, completed, kind="canceled")
if paused:
    return self._finish(task, TaskState.PAUSED, False, completed, kind="paused")
if first_error is not None:
    return self._fail_task(task, first_error, None,
                           state=TaskState.WAITING_FOR_NETWORK if retryable else TaskState.FAILED)
```

仅当所有文件项目成功或仅产生跳过／源保留警告后，再按已有顺序运行 `SourceKind.EMPTY_DIRECTORY`。仅当全部项目结果已落盘后，才依次转换任务为 `VERIFYING`、`COMMITTING` 与 `COMPLETED`／`COMPLETED_WITH_WARNINGS`。保留 queue 测试的“最多一个 RUNNING task”断言，不改变 `QueueCoordinator` 的锁。

- [ ] **Step 5：运行调度与安全语义回归。**

Run: `.venv/bin/pytest tests/unit/transfer/test_transfer_engine.py tests/unit/transfer/test_queue.py tests/integration/test_desktop_workflow.py -q`

Expected: PASS；并发 `1` 保持原顺序，并发 `2`／`4` 不超过上限，失败不会删除未验证源。

### Task 5：重试入口和桌面运行时的独立会话工厂

**Files:**

- Modify: `src/nasmove/transfer/retrying_runner.py:13-115`
- Modify: `src/nasmove/ui/desktop_app.py:130-194`
- Test: `tests/unit/transfer/test_retrying_runner.py`
- Test: `tests/unit/ui/test_desktop_app.py`

**Interfaces:**

- Produces: `EngineFactory = Callable[[TaskRecord, str], Engine]`。
- Produces: `RetryingTaskRunner` 的 `engine_factory: EngineFactory` 参数，不再预先创建或共享一个任务级 SMB session。
- Produces: desktop `build_engine(task: TaskRecord, password: str) -> TransferEngine`，其中 `item_worker_factory(task, password)` 每次创建一套新 gateway 与 session。

- [ ] **Step 1：写 runner 只传递非持久化密码、网络等待后重建 engine 的失败测试。**

```python
def test_runner_rebuilds_engine_with_task_and_keychain_password_after_network_wait() -> None:
    calls: list[tuple[TaskId, str]] = []

    class ResultsEngine:
        def run_task(self, task_id: TaskId, token: CancellationToken) -> TaskResult:
            del task_id, token
            return results.pop(0)

    def engine_factory(task, password):
        calls.append((task.id, password))
        return ResultsEngine()
    result = runner(engine_factory).run_task(TaskId("task-1"), CancellationToken())
    assert result.state is TaskState.COMPLETED
    assert calls == [(TaskId("task-1"), "memory-only-secret")] * 2
```

- [ ] **Step 2：运行 runner 测试并确认旧工厂仍要求 `SessionInfo`。**

Run: `.venv/bin/pytest tests/unit/transfer/test_retrying_runner.py -q`

Expected: FAIL，旧 `engine_factory(session)` 不能接收 task 与 password。

- [ ] **Step 3：把连接建立职责移入每项目工作器工厂。**

```python
def build_engine(task: TaskRecord, password: str) -> TransferEngine:
    def build_item_worker(worker_task: TaskRecord, worker_password: str) -> TransferItemWorker:
        gateway = SmbProtocolGateway()
        session = gateway.connect(worker_task.connection, worker_password)
        return TransferItemWorker(
            repository=repository,
            gateway=gateway,
            session=session,
            local=local,
            event_sink=events,
            progress=progress,
        )
    return TransferEngine(repository, item_worker_factory=build_item_worker, password=password, event_sink=events)
```

`RetryingTaskRunner.run_task` 每轮从 Keychain 读取密码，调用 `engine_factory(task, password).run_task(...)`，并在 `WAITING_FOR_NETWORK` 后按既有退避逻辑重建 engine。删除 runner 对共享 gateway 的 `connect()`／`reset_connection()` 调用；保留桌面 runtime 的主 gateway，供连接测试、目录浏览和任务规划使用。不得在闭包、事件或异常文本中保留 password。

- [ ] **Step 4：将每槽进度聚合改为锁保护的项目 offset map。**

```python
progress_lock = RLock()
copied: dict[TransferItemId, int] = {}
verified: dict[TransferItemId, int] = {}

def copy_progress(item: TransferItemRecord, offset: int) -> None:
    with progress_lock:
        copied[item.id] = offset
        copied_total = sum(copied.values())
        verified_total = sum(verified.values())
    progress.update(copied_bytes=copied_total, verified_bytes=verified_total)
```

对 verification 回调使用同一把锁，且只在锁外发布 UI progress snapshot。每个 worker 的 `close()` 必须在 session 释放后调用 `repository.release_thread_connection()`。

- [ ] **Step 5：运行 runner、运行时和隔离测试。**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest tests/unit/transfer/test_retrying_runner.py tests/unit/ui/test_desktop_app.py tests/unit/transfer/test_transfer_engine.py -q`

Expected: PASS；并发工作器的 gateway identity、session generation 和 repository connection identity 均不重复。

### Task 6：关闭、恢复与完整回归验证

**Files:**

- Modify: `src/nasmove/transfer/transfer_engine.py:141-360`
- Modify: `src/nasmove/ui/desktop_app.py:130-194`
- Test: `tests/unit/transfer/test_transfer_engine.py`
- Test: `tests/unit/ui/test_main_window.py`
- Test: `tests/integration/test_desktop_workflow.py`

**Interfaces:**

- Produces: `TransferEngine.shutdown(timeout_seconds: float = 2.0) -> bool`，请求停止派发并等待活动项目 worker 完成资源释放。
- Produces: desktop application shutdown 在关闭 repository 前调用 active engine／queue shutdown。

- [ ] **Step 1：写取消、暂停、网络失败和应用关闭的失败测试。**

```python
def test_parallel_cancel_waits_for_workers_before_task_is_canceled(parallel_engine) -> None:
    parallel_engine.token.request_cancel()
    result = parallel_engine.run_task(parallel_engine.task.id, parallel_engine.token)
    assert result.state is TaskState.CANCELED
    assert parallel_engine.active_worker_count == 0
    assert parallel_engine.repository.release_calls == parallel_engine.started_worker_count


def test_main_window_close_does_not_close_repository_while_parallel_engine_is_active(qtbot, window) -> None:
    window.queue_coordinator.shutdown = lambda: True
    window.close()
    assert window.application.repository_closed_after_workers is True
```

- [ ] **Step 2：运行可靠性测试并确认当前实现没有并发 worker shutdown 合约。**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest tests/unit/transfer/test_transfer_engine.py tests/unit/ui/test_main_window.py -q`

Expected: FAIL，缺少 `TransferEngine.shutdown` 或 worker 未完全关闭。

- [ ] **Step 3：实现关闭协调。**

```python
def shutdown(self, timeout_seconds: float = 2.0) -> bool:
    self._stop_scheduling.set()
    deadline = monotonic() + timeout_seconds
    for future in tuple(self._active_futures):
        future.cancel()
    return self._wait_for_active_workers(max(0.0, deadline - monotonic()))
```

将该方法接入 queue／application 的关闭路径：先设置共享取消请求并停止新增项目，再等待已启动 worker 在检查点结束、调用 `close()`、释放 SQLite 线程连接和 SMB session，最后允许 repository close。超时必须保留窗口并提示正在停止传输，不能销毁仍在运行的线程。

- [ ] **Step 4：验证恢复时不删除未校验源。**

```python
def test_parallel_verification_failure_after_restart_retains_source(recovery_runtime) -> None:
    result = recovery_runtime.resume_interrupted_task()
    assert result.state is TaskState.FAILED
    assert recovery_runtime.local.trash_calls == []
```

- [ ] **Step 5：运行全部测试、静态检查和 diff 检查。**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q`

Expected: PASS。

Run: `.venv/bin/ruff check src tests && .venv/bin/mypy src && git diff --check`

Expected: `All checks passed!`、`Success: no issues found`，并且无 whitespace 错误。

- [ ] **Step 6：进行真实界面烟测并记录发布边界。**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m nasmove.ui.desktop_app`

Expected: 应用可启动；配置页在 `1100×760` 和 `1600×1000` 可读；任务队列仍只显示一个活动任务。

记录：本地 mock／fixture 并发验证不替代真实 Synology 的大文件与大量小文件验收；正式发布仍需独立验证 NAS 会话上限、网络中断恢复、软删除和 macOS 签名／公证。

## Plan Self-Review

- Spec coverage：Task 1 覆盖默认值、范围、schema 6、任务快照与密码不落库；Task 2 覆盖配置 UI；Task 3 至 5 覆盖独立 session、SQLite connection、进度、并发上限、串行队列、空目录及网络重试；Task 6 覆盖暂停、取消、关闭、恢复和 UI 回归。
- 占位扫描：已检查本计划，不含未定义的后续工作占位。
- Type consistency：`ConnectionConfig.max_parallel_items`、`ItemRunOutcome`、`ItemWorkerFactory`、`TransferEngine.shutdown` 和 `EngineFactory(TaskRecord, str)` 在所有任务中使用同一名称和签名。
