# 群晖 NAS 可靠文件迁移工具 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个稳定性优先的 macOS Python 桌面应用，通过 SMB2／SMB3 将本地大文件或大量文件可靠地复制或安全迁移到群晖 NAS。

**Architecture:** 应用采用领域状态机约束传输、校验、提交和源删除顺序；PySide6 UI 只发出命令并订阅不可变事件，唯一后台执行器串行消费 SQLite 持久化任务队列。`smbprotocol` 适配层负责远端临时文件、偏移写、刷新、截断、重连和同目录重命名，恢复协调器通过本地数据库、源文件和远端状态三方核验确定安全断点。

**Tech Stack:** Python 3.12、PySide6／Qt Widgets、`smbprotocol`／`smbclient`、SQLite／`sqlite3`、Python `keyring`／macOS Keychain、`hashlib.sha256`、pytest、pytest-qt、Ruff、mypy、`pyside6-deploy`。

**Spec:** `docs/superpowers/specs/2026-09-05-nas-reliable-transfer-design.md`

## Global Constraints

- 首要质量属性顺序为：正确性、稳定性、可维护性、一致性、性能、优雅性。
- Python 版本固定为 3.12 系列；不使用自由线程构建。
- macOS MVP 使用 PySide6 Qt Widgets，所有 SMB、哈希、扫描和 SQLite 长事务必须离开 UI 主线程。
- SMB 主通道使用 `smbprotocol` 直连，不依赖 Finder 预挂载共享目录。
- 永久禁用 SMB1；默认要求 SMB3 加密和安全协商。
- 任务队列可保存多个任务，但同一时刻只能有一个活动任务和一个活动 SMB 写流。
- 默认保留源目录层级；多个独立来源保留各自顶层名称。
- 默认冲突策略为自动重命名，最终名称一经分配必须持久化。
- 基础 I/O 块为 4 MiB；持久化检查点间隔为 64 MiB。
- 远端半成品命名为 `.nasmove-<transfer_item_uuid>.part`，仅在完整校验成功后同目录提交。
- 默认完整回读 SHA-256 校验；移动任务强制完整校验且不能关闭。
- 未完整校验、未成功提交、源文件已变化或恢复代次已变化时，不得删除源文件。
- 密码只进入进程内存或 macOS Keychain；数据库、日志、配置和报告不得保存密码。
- 未通过真实群晖偏移写、刷新、截断和同目录重命名能力探测前，不实现或启用安全移动。
- 当前目录不是 Git 仓库。执行前必须由用户明确决定是否初始化 Git；未获授权时跳过各任务的提交命令，但保留完整验证记录。

---

## 1．范围检查与交付顺序

设计包含 UI、传输、恢复、持久化和安全模块，但它们围绕同一个不可拆分的不变量工作：只有已完整校验并成功提交的目标才能授权删除源文件。因此保留一份实施计划，通过硬门槛和独立评审任务逐步交付。

实施顺序不得调整为“先做完整 UI，再补可靠传输”。阶段 0 的真实 NAS 能力探针不通过时，必须停止后续实现并回到设计评审。

## 2．目标文件结构

```text
NasMove/
├── pyproject.toml
├── requirements.lock
├── README.md
├── src/nasmove/
│   ├── __init__.py
│   ├── app.py
│   ├── core/
│   │   ├── errors.py
│   │   ├── model.py
│   │   ├── ports.py
│   │   ├── retry.py
│   │   ├── states.py
│   │   └── transitions.py
│   ├── planning/
│   │   ├── addresses.py
│   │   ├── conflicts.py
│   │   ├── paths.py
│   │   └── task_planner.py
│   ├── persistence/
│   │   ├── schema.py
│   │   └── sqlite_repository.py
│   ├── security/
│   │   ├── keychain_store.py
│   │   └── redacted_logging.py
│   ├── localio/
│   │   ├── files.py
│   │   └── hashing.py
│   ├── smb/
│   │   ├── capability_probe.py
│   │   ├── error_mapping.py
│   │   └── smbprotocol_gateway.py
│   ├── transfer/
│   │   ├── checkpoint_writer.py
│   │   ├── commit.py
│   │   ├── deletion.py
│   │   ├── progress.py
│   │   ├── recovery.py
│   │   ├── transfer_engine.py
│   │   └── verification.py
│   └── ui/
│       ├── connection_page.py
│       ├── main_window.py
│       ├── source_page.py
│       ├── target_page.py
│       ├── task_page.py
│       ├── view_models.py
│       └── worker.py
├── tools/
│   └── smb_capability_probe.py
└── tests/
    ├── conftest.py
    ├── unit/
    ├── integration/
    ├── fault/
    └── fixtures/
        ├── builders.py
        ├── fake_smb.py
        ├── security.py
        ├── transfer.py
        ├── application.py
        ├── ui.py
        ├── synology.py
        └── fault_proxy.py
```

职责边界：

- `core` 不导入 PySide6、SQLite、`keyring` 或 `smbprotocol`。
- `planning` 只生成不可变任务计划，不执行文件内容传输。
- `persistence` 只保存和查询状态，不决定状态迁移是否合法。
- `smb` 只封装 SMB 行为和错误映射，不决定是否删除源文件。
- `transfer` 编排传输协议，删除授权必须调用 `core.transitions`。
- `ui` 不直接调用 `os.remove`、`smbclient.open_file` 或 SQL。

## 3．统一接口清单

后续任务必须使用下列名称，不自行创建同义接口：

```python
from pathlib import Path
from typing import BinaryIO, ContextManager, Protocol, Sequence

class TaskRepository(Protocol):
    def create_task(self, task: "TaskRecord", items: Sequence["TransferItemRecord"]) -> None: ...
    def get_task(self, task_id: "TaskId") -> "TaskRecord": ...
    def get_item(self, item_id: "TransferItemId") -> "TransferItemRecord": ...
    def next_queued_task(self) -> "TaskRecord | None": ...
    def transition_task(self, task_id: "TaskId", expected: "TaskState", target: "TaskState") -> None: ...
    def transition_item(self, item_id: "TransferItemId", expected: "ItemState", target: "ItemState") -> None: ...
    def update_item_metadata(self, item: "TransferItemRecord", expected_revision: int) -> None: ...
    def save_checkpoint(self, checkpoint: "Checkpoint") -> None: ...
    def checkpoints_desc(self, item_id: "TransferItemId") -> list["Checkpoint"]: ...
    def list_incomplete_tasks(self) -> list["TaskRecord"]: ...
    def mark_active_tasks_interrupted(self) -> int: ...
    def reorder_queued_tasks(self, task_ids: Sequence["TaskId"]) -> None: ...

class CredentialStore(Protocol):
    def get_password(self, profile_id: "ConnectionProfileId") -> str | None: ...
    def set_password(self, profile_id: "ConnectionProfileId", password: str) -> None: ...
    def delete_password(self, profile_id: "ConnectionProfileId") -> None: ...

class LocalFileGateway(Protocol):
    def fingerprint(self, path: Path) -> "SourceFingerprint": ...
    def open_read(self, path: Path) -> ContextManager[BinaryIO]: ...
    def remove_file(self, path: Path) -> None: ...
    def remove_empty_dir(self, path: Path) -> None: ...

class SmbGateway(Protocol):
    def connect(self, config: "ConnectionConfig", password: str) -> "SessionInfo": ...
    def disconnect(self) -> None: ...
    def reset_connection(self) -> None: ...
    def stat(self, path: "RemotePath") -> "RemoteStat | None": ...
    def list_dir(self, path: "RemotePath") -> list["RemoteEntry"]: ...
    def open_read(self, path: "RemotePath") -> ContextManager[BinaryIO]: ...
    def open_update(self, path: "RemotePath") -> ContextManager[BinaryIO]: ...
    def create_exclusive(self, path: "RemotePath") -> ContextManager[BinaryIO]: ...
    def truncate(self, path: "RemotePath", size: int) -> None: ...
    def rename_exclusive(self, source: "RemotePath", target: "RemotePath") -> None: ...
    def remove_file(self, path: "RemotePath") -> None: ...
    def make_dir(self, path: "RemotePath") -> None: ...
    def free_space(self, path: "RemotePath") -> int: ...

class EventSink(Protocol):
    def publish(self, event: "TransferEvent") -> None: ...
```

计划正文中的省略号仅出现在本节的 `Protocol` 签名展示中，表示接口方法体由适配器实现；任务步骤中的生产代码不得保留省略号或空实现。

### 3.1．测试支撑契约

测试夹具只模拟端口并记录调用，不得复制生产状态机或安全判断。`tests/conftest.py` 通过 `pytest_plugins` 注册下列夹具模块；每个任务在首次使用夹具时创建对应文件，后续任务只按已声明契约扩展：

- `tests/fixtures/builders.py`：提供 `make_task_record()`、`make_transfer_item_record()`、`snapshot()` 和 `result_with_source_retained()`；返回值使用真实领域记录，不返回无约束字典。
- `tests/fixtures/fake_smb.py`：提供 `RecordingSmbGateway`、`ShortWritingStream` 和 `FakeNtStatusError`。前两者记录调用顺序与字节，后者只暴露错误映射所需的 NTSTATUS 值。
- `tests/fixtures/security.py`：提供 `fake_keyring`，可设置 `backend_name`，并在内存中记录 get／set／delete，不接触用户 Keychain。
- `tests/fixtures/transfer.py`：提供 `fake_dependencies`、`recovery_fixture`、`verification_fixture`、`commit_fixture`、`deletion_fixture`、`engine_fixture` 和 `queue_fixture`。夹具组合真实待测服务与内存端口；`deletion_fixture.replace_item(**changes)` 和 `replace_session(**changes)` 使用 `dataclasses.replace()` 替换不可变记录。
- `tests/fixtures/application.py`：提供 `app_fixture`，可植入遗留运行任务，并记录“标记中断 → 核验恢复 → 入队”的调用顺序。
- `tests/fixtures/ui.py`：提供 `ui_fixture`，持有 `qt_application`、页面和记录调用线程 ID 的假服务。
- `tests/fixtures/synology.py`：提供 `synology_fixture`；仅在 `NASMOVE_TEST_SYNOLOGY=1` 且专用测试共享配置齐全时可创建，否则跳过测试。它不得读取普通应用配置或生产共享路径。
- `tests/fixtures/fault_proxy.py`：提供只面向专用测试共享的 `FaultProxy`，用于断开、延迟和恢复 TCP 通道。

所有测试辅助对象必须有类型标注。测试引用的新夹具名若未在本节登记，须先更新本契约和对应任务的 **Files** 清单。

---

### Task 1：建立最小工程与质量门禁

**Files:**
- Create: `pyproject.toml`
- Create: `requirements.lock`
- Create: `README.md`
- Create: `src/nasmove/__init__.py`
- Create: `tests/unit/test_package.py`

**Interfaces:**
- Consumes: 已批准设计文档。
- Produces: 可安装的 `nasmove` 包、统一测试与静态检查命令。

- [ ] **Step 1：写包元数据失败测试**

```python
from importlib.metadata import version

import nasmove


def test_package_and_module_versions_match() -> None:
    assert nasmove.__version__ == version("nasmove")
```

- [ ] **Step 2：运行测试并确认失败**

Run: `python3.12 -m pytest tests/unit/test_package.py -v`

Expected: FAIL，错误包含 `ModuleNotFoundError: No module named 'nasmove'`。

- [ ] **Step 3：创建最小包与项目配置**

`src/nasmove/__init__.py`：

```python
from importlib.metadata import version

__version__ = version("nasmove")
```

`pyproject.toml` 的关键内容：

```toml
[build-system]
requires = ["setuptools>=77,<82"]
build-backend = "setuptools.build_meta"

[project]
name = "nasmove"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "PySide6>=6.10,<7",
  "smbprotocol>=1.15,<2",
  "keyring>=25,<27",
]

[project.optional-dependencies]
dev = [
  "mypy>=1.18,<2",
  "pip-tools>=7.5,<8",
  "pytest>=8.4,<10",
  "pytest-qt>=4.5,<5",
  "ruff>=0.13,<1",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.mypy]
python_version = "3.12"
strict = true
packages = ["nasmove"]
```

生成锁文件：

Run: `python3.12 -m piptools compile --extra dev --generate-hashes --output-file requirements.lock pyproject.toml`

`README.md` 只写项目目标、非目标、当前开发状态和设计文档链接，不写未实现的使用说明。

- [ ] **Step 4：安装并运行质量门禁**

Run: `python3.12 -m pip install -e '.[dev]'`

Run: `python3.12 -m pytest tests/unit/test_package.py -v`

Run: `python3.12 -m ruff check src tests`

Run: `python3.12 -m mypy src`

Expected: 所有命令退出码为 0。

- [ ] **Step 5：提交工程基础**

```bash
git add pyproject.toml requirements.lock README.md src/nasmove/__init__.py tests/unit/test_package.py
git commit -m "build: establish Python project quality gates"
```

---

### Task 2：定义领域模型、状态和迁移约束

**Files:**
- Create: `src/nasmove/core/errors.py`
- Create: `src/nasmove/core/model.py`
- Create: `src/nasmove/core/states.py`
- Create: `src/nasmove/core/transitions.py`
- Create: `tests/fixtures/builders.py`
- Create: `tests/unit/core/test_transitions.py`
- Create: `tests/unit/core/test_deletion_authorization.py`

**Interfaces:**
- Consumes: 无运行时基础设施依赖。
- Produces: `TaskId`、`TransferItemId`、`ConnectionConfig`、`SourceFingerprint`、`Checkpoint`、`TaskState`、`ItemState`、`assert_item_transition()`、`authorize_source_delete()`。

- [ ] **Step 1：写禁止越级迁移的失败测试**

```python
import pytest

from nasmove.core.errors import InvalidTransition
from nasmove.core.states import ItemState
from nasmove.core.transitions import assert_item_transition


def test_unverified_item_cannot_jump_to_delete_authorized() -> None:
    with pytest.raises(InvalidTransition):
        assert_item_transition(ItemState.TRANSFERRED, ItemState.SOURCE_DELETE_AUTHORIZED)
```

- [ ] **Step 2：运行测试并确认失败**

Run: `python3.12 -m pytest tests/unit/core/test_transitions.py -v`

Expected: FAIL，错误包含 `ModuleNotFoundError: No module named 'nasmove.core'`。

- [ ] **Step 3：实现状态枚举和显式迁移图**

`src/nasmove/core/states.py`：

```python
from enum import StrEnum


class TaskState(StrEnum):
    DRAFT = "draft"
    PREFLIGHT = "preflight"
    QUEUED = "queued"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    WAITING_FOR_NETWORK = "waiting_for_network"
    PAUSED = "paused"
    VERIFYING = "verifying"
    COMMITTING = "committing"
    DELETING_SOURCE = "deleting_source"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    CANCELED = "canceled"


class ItemState(StrEnum):
    PLANNED = "planned"
    TRANSFERRING = "transferring"
    TRANSFERRED = "transferred"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    COMMITTED = "committed"
    SOURCE_DELETE_AUTHORIZED = "source_delete_authorized"
    DONE = "done"
    INTERRUPTED = "interrupted"
    WAITING_RETRY = "waiting_retry"
    SOURCE_CHANGED = "source_changed"
    VERIFY_FAILED = "verify_failed"
    SOURCE_RETAINED = "source_retained"
    SKIPPED = "skipped"
```

`src/nasmove/core/transitions.py` 使用常量字典声明每个状态允许到达的目标；`assert_item_transition(current, target)` 不在字典集合中时抛出 `InvalidTransition`。

- [ ] **Step 4：写删除授权失败测试**

```python
import pytest

from nasmove.core.errors import UnsafeSourceDeletion
from nasmove.core.model import DeletionEvidence
from nasmove.core.transitions import authorize_source_delete


def test_session_generation_change_revokes_delete_authorization() -> None:
    evidence = DeletionEvidence(
        source_unchanged=True,
        full_hash_verified=True,
        target_committed=True,
        verified_session_generation=4,
        current_session_generation=5,
    )
    with pytest.raises(UnsafeSourceDeletion):
        authorize_source_delete(evidence)
```

- [ ] **Step 5：实现不可变领域记录和删除授权**

`TaskRecord` 和 `TransferItemRecord` 包含单调递增的 `revision` 字段，所有领域记录使用 `@dataclass(frozen=True, slots=True)`。`DeletionEvidence` 同样不可变；`authorize_source_delete()` 必须逐项要求：`source_unchanged`、`full_hash_verified`、`target_committed` 均为真，且两个 session generation 相等；成功返回不可变的 `SourceDeleteAuthorization`，包含源指纹、目标路径、SHA-256 和恢复代次。

- [ ] **Step 6：运行领域测试与静态检查**

Run: `python3.12 -m pytest tests/unit/core -v`

Run: `python3.12 -m mypy src/nasmove/core`

Expected: 全部通过，且覆盖每个禁止越级路径。

- [ ] **Step 7：提交领域不变量**

```bash
git add src/nasmove/core tests/fixtures/builders.py tests/unit/core
git commit -m "feat: define transfer safety state machine"
```

---

### Task 3：建立 SMB 接口并完成真实群晖能力探针

**Files:**
- Create: `src/nasmove/core/ports.py`
- Create: `src/nasmove/smb/error_mapping.py`
- Create: `src/nasmove/smb/smbprotocol_gateway.py`
- Create: `src/nasmove/smb/capability_probe.py`
- Create: `tools/smb_capability_probe.py`
- Create: `tests/fixtures/fake_smb.py`
- Create: `tests/unit/smb/test_capability_probe.py`
- Create: `tests/integration/test_smb_capability_probe.py`

**Interfaces:**
- Consumes: `ConnectionConfig`、`RemotePath`。
- Produces: 本计划第 3 节中的 `SmbGateway`，以及 `SmbCapabilityProbe.run(target: RemotePath) -> CapabilityReport`。

- [ ] **Step 1：写能力探针调用顺序测试**

```python
from nasmove.core.model import RemotePath
from nasmove.smb.capability_probe import SmbCapabilityProbe
from tests.fixtures.fake_smb import RecordingSmbGateway


def test_probe_requires_random_write_flush_truncate_rename_and_cleanup() -> None:
    gateway = RecordingSmbGateway()
    report = SmbCapabilityProbe(gateway).run(RemotePath("archive/incoming"))
    assert report.random_write is True
    assert report.flush is True
    assert report.truncate is True
    assert report.rename_exclusive is True
    assert report.cleanup is True
    assert gateway.calls == [
        "create_exclusive",
        "write_initial",
        "flush",
        "reopen_update",
        "seek_append",
        "truncate",
        "rename_exclusive",
        "open_read",
        "remove_file",
    ]
```

- [ ] **Step 2：运行单元测试并确认失败**

Run: `python3.12 -m pytest tests/unit/smb/test_capability_probe.py -v`

Expected: FAIL，错误包含 `ModuleNotFoundError: No module named 'nasmove.smb'`。

- [ ] **Step 3：实现最小 SMB 适配器和能力探针**

`SmbProtocolGateway` 必须：

- 使用规范化 UNC 路径调用 `smbclient`。
- `connect()` 返回协商方言、是否签名、是否加密和自增 `session_generation`。
- `open_update()` 使用可读写二进制模式，暴露 `seek()`、短写和 `flush()`。
- `reset_connection()` 调用 `smbclient.reset_connection_cache()`，并使旧句柄不可复用。
- `rename_exclusive()` 在目标存在时抛出 `TargetExistsError`，不覆盖目标。

`SmbCapabilityProbe.run()` 使用随机 UUID 创建不超过 1 MiB 的探测文件，验证两段固定字节、偏移追加、截断、同目录重命名和完整清理，并返回每一项布尔结果及脱敏错误码。

- [ ] **Step 4：实现只通过 `getpass` 接收密码的探针 CLI**

```python
password = getpass.getpass("NAS password: ")
config = ConnectionConfig(
    host=args.host,
    port=args.port,
    share=args.share,
    username=args.username,
    domain=args.domain,
    require_encryption=not args.allow_unencrypted,
)
report = run_probe(config=config, password=password, target=args.target)
print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
```

命令行参数不得提供 `--password`，防止密码进入 shell history 和进程列表。

- [ ] **Step 5：运行单元测试和受控 Samba 集成测试**

Run: `python3.12 -m pytest tests/unit/smb/test_capability_probe.py -v`

Run: `NASMOVE_TEST_SMB=1 python3.12 -m pytest tests/integration/test_smb_capability_probe.py -v`

Expected: 测试共享返回五项能力均为真，探测目录中不存在残留文件。

- [ ] **Step 6：在用户的真实群晖目标目录运行探针**

Run: `python3.12 tools/smb_capability_probe.py`

Expected: JSON 中 `random_write`、`flush`、`truncate`、`rename_exclusive`、`cleanup` 全部为 `true`；NAS 目标目录无残留 `.nasmove-probe-*` 文件。

CLI 在参数缺失时依次交互询问地址、端口、共享名、用户名、域和非生产测试目录，密码只通过 `getpass` 获取。任一核心能力为 `false` 时，停止 Task 4 及后续任务，保存脱敏探针结果并回到设计评审。

- [ ] **Step 7：提交能力探针**

```bash
git add src/nasmove/core/ports.py src/nasmove/smb tools/smb_capability_probe.py tests/fixtures/fake_smb.py tests/unit/smb tests/integration/test_smb_capability_probe.py
git commit -m "feat: verify Synology SMB durability capabilities"
```

---

### Task 4：实现地址、路径和冲突规划

**Files:**
- Create: `src/nasmove/planning/addresses.py`
- Create: `src/nasmove/planning/paths.py`
- Create: `src/nasmove/planning/conflicts.py`
- Create: `src/nasmove/planning/task_planner.py`
- Create: `tests/unit/planning/test_addresses.py`
- Create: `tests/unit/planning/test_paths.py`
- Create: `tests/unit/planning/test_conflicts.py`
- Create: `tests/unit/planning/test_task_planner.py`

**Interfaces:**
- Consumes: `ConnectionConfig`、`LocalFileGateway`、`SmbGateway`。
- Produces: `parse_smb_address(value: str) -> ParsedSmbAddress`、`normalize_remote_path(value: str) -> RemotePath`、`allocate_name(name: str, occupied: set[str]) -> str`、`TaskPlanner.plan(request: PlanRequest) -> PlannedTask`。

- [ ] **Step 1：写地址与逃逸路径失败测试**

```python
import pytest

from nasmove.core.errors import InvalidRemotePath
from nasmove.planning.addresses import parse_smb_address
from nasmove.planning.paths import normalize_remote_path


def test_smb_url_is_split_into_host_share_and_path() -> None:
    parsed = parse_smb_address("smb://nas.local/archive/photos/2026")
    assert parsed.host == "nas.local"
    assert parsed.share == "archive"
    assert parsed.initial_path == "photos/2026"


def test_parent_escape_is_rejected() -> None:
    with pytest.raises(InvalidRemotePath):
        normalize_remote_path("archive/../../private")
```

- [ ] **Step 2：运行测试并确认失败**

Run: `python3.12 -m pytest tests/unit/planning -v`

Expected: FAIL，错误包含缺少 `nasmove.planning`。

- [ ] **Step 3：实现确定性解析和规范化**

地址解析支持 IPv4、方括号 IPv6、主机名、`.local` 和 `smb://`。远端路径统一使用 `/` 作为内部表示，拒绝空组件、`.`、`..` 和控制字符；冲突比较键使用 Unicode NFC 加 `casefold()`。

- [ ] **Step 4：写自动重命名测试**

```python
from nasmove.planning.conflicts import allocate_name


def test_conflict_suffix_preserves_extension() -> None:
    occupied = {"movie.mov", "movie (1).mov"}
    assert allocate_name("movie.mov", occupied) == "movie (2).mov"
```

- [ ] **Step 5：实现流式任务规划**

`TaskPlanner` 逐目录扫描并每 1,000 项批量交给仓储保存。普通目录保留顶层名称；符号链接标记 `SKIPPED`；FIFO、Socket 和设备文件标记 `SKIPPED`；普通文件记录设备号、inode、大小和纳秒修改时间。空间要求按逻辑大小加 `max(1 GiB, 总大小的 5％)` 计算。

- [ ] **Step 6：运行规划测试**

Run: `python3.12 -m pytest tests/unit/planning -v`

Expected: 地址、Unicode、路径逃逸、多个顶层来源、符号链接和自动重命名测试全部通过。

- [ ] **Step 7：提交规划模块**

```bash
git add src/nasmove/planning tests/unit/planning
git commit -m "feat: plan safe source and NAS target mappings"
```

---

### Task 5：实现 SQLite 崩溃安全仓储

**Files:**
- Create: `src/nasmove/persistence/schema.py`
- Create: `src/nasmove/persistence/sqlite_repository.py`
- Create: `tests/unit/persistence/test_schema.py`
- Create: `tests/unit/persistence/test_repository.py`
- Create: `tests/fault/test_sqlite_crash_windows.py`

**Interfaces:**
- Consumes: `TaskRecord`、`TransferItemRecord`、`Checkpoint`、`TaskState`、`ItemState`。
- Produces: 本计划第 3 节中的 `TaskRepository` 实现 `SqliteTaskRepository`。

- [ ] **Step 1：写检查点原子性失败测试**

```python
from nasmove.core.model import Checkpoint
from nasmove.persistence.sqlite_repository import SqliteTaskRepository
from tests.fixtures.builders import make_task_record, make_transfer_item_record


def test_checkpoint_and_item_offset_commit_atomically(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "nasmove.db")
    task = make_task_record()
    item = make_transfer_item_record(task_id=task.id, confirmed_offset=0)
    repository.create_task(task, [item])
    repository.save_checkpoint(
        Checkpoint(
            item_id=item.id,
            confirmed_offset=64 * 1024 * 1024,
            remote_size=64 * 1024 * 1024,
            window_start=60 * 1024 * 1024,
            window_length=4 * 1024 * 1024,
            window_sha256="a" * 64,
            session_generation=1,
        )
    )
    assert repository.get_item(item.id).confirmed_offset == 64 * 1024 * 1024
    assert repository.checkpoints_desc(item.id)[0].confirmed_offset == 64 * 1024 * 1024
```

- [ ] **Step 2：运行测试并确认失败**

Run: `python3.12 -m pytest tests/unit/persistence -v`

Expected: FAIL，错误包含缺少 `nasmove.persistence`。

- [ ] **Step 3：实现 schema 和仓储事务**

数据库初始化必须执行：

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

创建 `schema_meta`、`connection_profiles`、`tasks`、`transfer_items`、`checkpoints`、`attempts` 和 `events` 表。`save_checkpoint()` 在一个 `BEGIN IMMEDIATE` 事务内插入检查点并更新文件项确认偏移；异常时回滚两者。

- [ ] **Step 4：实现比较并交换状态迁移**

`transition_item(item_id, expected, target)` 的 SQL 更新必须包含 `WHERE id = ? AND state = ?`。受影响行数不是 1 时抛出 `ConcurrentStateChange`。调用 SQL 前执行 `assert_item_transition()`。

`update_item_metadata(item, expected_revision)` 必须包含 `WHERE id = ? AND revision = ?`，只允许更新最终路径、摘要、长度、确认偏移和校验代次等非状态字段；若传入记录的状态与数据库不同则拒绝。`mark_active_tasks_interrupted()` 在单个事务中把遗留活动任务标为 `INTERRUPTED` 并返回数量。`reorder_queued_tasks()` 只接受当前全部等待任务 ID 的一个无重复排列，并在单个事务中更新序号。

- [ ] **Step 5：运行断电窗口测试**

Run: `python3.12 -m pytest tests/fault/test_sqlite_crash_windows.py -v`

Expected: 在检查点插入前、文件项更新前和提交前注入异常时，重新打开数据库后不存在半事务状态。

- [ ] **Step 6：运行仓储测试和数据库完整性检查**

Run: `python3.12 -m pytest tests/unit/persistence tests/fault/test_sqlite_crash_windows.py -v`

Run: `sqlite3 .test-data/nasmove.db 'PRAGMA integrity_check;'`

Expected: pytest 全部通过；SQLite 输出 `ok`。

- [ ] **Step 7：提交持久化模块**

```bash
git add src/nasmove/persistence tests/unit/persistence tests/fault/test_sqlite_crash_windows.py
git commit -m "feat: persist crash-safe transfer state"
```

---

### Task 6：实现 Keychain 与日志脱敏

**Files:**
- Create: `src/nasmove/security/keychain_store.py`
- Create: `src/nasmove/security/redacted_logging.py`
- Create: `tests/conftest.py`
- Create: `tests/fixtures/security.py`
- Create: `tests/unit/security/test_keychain_store.py`
- Create: `tests/unit/security/test_redacted_logging.py`

**Interfaces:**
- Consumes: `ConnectionProfileId`、Python `keyring`。
- Produces: `MacOSKeychainCredentialStore`、`RedactingFilter`、`configure_logging(log_dir: Path) -> logging.Logger`。

- [ ] **Step 1：写后端拒绝与脱敏失败测试**

```python
import logging

import pytest

from nasmove.core.errors import UnsafeCredentialBackend
from nasmove.security.keychain_store import MacOSKeychainCredentialStore
from nasmove.security.redacted_logging import RedactingFilter


def test_non_macos_keyring_backend_is_rejected(fake_keyring) -> None:
    fake_keyring.backend_name = "keyrings.alt.file.PlaintextKeyring"
    with pytest.raises(UnsafeCredentialBackend):
        MacOSKeychainCredentialStore(fake_keyring)


def test_password_and_unc_credentials_are_redacted() -> None:
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "password=secret smb://u:p@nas/a", (), None)
    RedactingFilter().filter(record)
    assert "secret" not in record.getMessage()
    assert "u:p" not in record.getMessage()
```

- [ ] **Step 2：运行测试并确认失败**

Run: `python3.12 -m pytest tests/unit/security -v`

Expected: FAIL，错误包含缺少 `nasmove.security`。

- [ ] **Step 3：实现 Keychain 硬门禁**

`MacOSKeychainCredentialStore` 初始化时检查后端模块为 `keyring.backends.macOS`，固定 service 为 `com.nasmove.smb`，account 使用连接配置 UUID。后端不安全时禁用保存并抛出 `UnsafeCredentialBackend`，不回退到文件存储。

- [ ] **Step 4：实现日志结构化脱敏**

`RedactingFilter` 处理 `password`、`passwd`、`authorization`、`ntlm`、`ticket`、`session_key` 和带用户信息的 URL；结构化上下文只接受 task ID、item ID、错误类别和系统错误码。默认日志路径为 `~/Library/Logs/NasMove/nasmove.log`，文件权限为 `0o600`。

- [ ] **Step 5：运行安全测试**

Run: `python3.12 -m pytest tests/unit/security -v`

Run: `python3.12 -m ruff check src/nasmove/security tests/unit/security`

Expected: 全部通过；测试日志不包含测试密码、完整用户名或 URL 凭据。

- [ ] **Step 6：提交安全基础设施**

```bash
git add src/nasmove/security tests/conftest.py tests/fixtures/security.py tests/unit/security
git commit -m "feat: secure credentials and redact diagnostics"
```

---

### Task 7：实现本地文件指纹和 SHA-256

**Files:**
- Create: `src/nasmove/localio/files.py`
- Create: `src/nasmove/localio/hashing.py`
- Create: `tests/unit/localio/test_files.py`
- Create: `tests/unit/localio/test_hashing.py`

**Interfaces:**
- Consumes: `SourceFingerprint`。
- Produces: `PosixLocalFileGateway`、`sha256_stream(stream: BinaryIO, block_size: int = 4 * 1024 * 1024) -> HashResult`、`sha256_range(stream: BinaryIO, start: int, length: int) -> str`。

- [ ] **Step 1：写文件变化检测测试**

```python
from nasmove.localio.files import PosixLocalFileGateway


def test_fingerprint_changes_when_content_changes(tmp_path) -> None:
    path = tmp_path / "source.bin"
    path.write_bytes(b"first")
    gateway = PosixLocalFileGateway()
    before = gateway.fingerprint(path)
    path.write_bytes(b"second")
    after = gateway.fingerprint(path)
    assert before != after
```

- [ ] **Step 2：写分块哈希测试**

```python
import hashlib
import io

from nasmove.localio.hashing import sha256_stream


def test_stream_hash_reads_until_eof() -> None:
    payload = b"abc" * 1_000_000
    result = sha256_stream(io.BytesIO(payload), block_size=4096)
    assert result.byte_count == len(payload)
    assert result.hexdigest == hashlib.sha256(payload).hexdigest()
```

- [ ] **Step 3：运行测试并确认失败**

Run: `python3.12 -m pytest tests/unit/localio -v`

Expected: FAIL，错误包含缺少 `nasmove.localio`。

- [ ] **Step 4：实现本地文件网关与哈希**

`fingerprint()` 使用 `os.stat(..., follow_symlinks=False)` 获取设备号、inode、类型、逻辑大小和 `st_mtime_ns`。`open_read()` 打开二进制只读句柄；删除文件和删除空目录使用两个不同方法。哈希函数固定使用 SHA-256，循环读取到 EOF，并返回摘要与实际读取字节数。

- [ ] **Step 5：运行本地 I/O 测试**

Run: `python3.12 -m pytest tests/unit/localio -v`

Expected: 普通文件、空文件、变化文件、符号链接拒绝和分块哈希全部通过。

- [ ] **Step 6：提交本地文件模块**

```bash
git add src/nasmove/localio tests/unit/localio
git commit -m "feat: fingerprint and hash local sources"
```

---

### Task 8：完善 SMB 网关与错误分类

**Files:**
- Modify: `src/nasmove/smb/smbprotocol_gateway.py`
- Modify: `src/nasmove/smb/error_mapping.py`
- Modify: `tests/fixtures/fake_smb.py`
- Create: `tests/unit/smb/test_error_mapping.py`
- Create: `tests/integration/test_smb_gateway.py`

**Interfaces:**
- Consumes: `ConnectionConfig`、`RemotePath`、`TransferErrorCategory`。
- Produces: 完整 `SmbGateway` 实现、`map_smb_error(error: BaseException) -> TransferFailure`。

- [ ] **Step 1：写短写与连接重置测试**

```python
from nasmove.smb.smbprotocol_gateway import write_all
from tests.fixtures.fake_smb import ShortWritingStream


def test_write_all_retries_short_writes() -> None:
    stream = ShortWritingStream(max_bytes_per_call=3)
    write_all(stream, b"abcdefgh")
    assert stream.value == b"abcdefgh"
    assert stream.write_calls == 3
```

- [ ] **Step 2：写错误分类测试**

```python
from nasmove.core.errors import TransferErrorCategory
from nasmove.smb.error_mapping import map_smb_error
from tests.fixtures.fake_smb import FakeNtStatusError


def test_access_denied_is_not_retryable() -> None:
    failure = map_smb_error(FakeNtStatusError("STATUS_ACCESS_DENIED"))
    assert failure.category is TransferErrorCategory.PERMISSION
    assert failure.retryable is False
```

- [ ] **Step 3：实现网关完整行为**

所有公开方法先构造规范化 UNC 路径。文件打开均返回上下文管理器；`disconnect()` 和 `reset_connection()` 幂等。写入使用 `write_all()` 处理短写；`rename_exclusive()` 在重命名前和失败后均查询目标，不能把目标存在错误误判为网络错误。

- [ ] **Step 4：实现错误映射表**

至少映射：连接重置、超时、DNS 失败、认证失败、账号锁定、访问拒绝、磁盘满、配额超限、路径不存在、目标存在、文件被占用、名称非法和协议不支持。只有网络瞬时错误标记 `retryable=True`。

- [ ] **Step 5：运行 SMB 单元与集成测试**

Run: `python3.12 -m pytest tests/unit/smb tests/integration/test_smb_gateway.py -v`

Expected: 短写、随机偏移、刷新、截断、独占创建、独占重命名和错误映射全部通过。

- [ ] **Step 6：提交 SMB 适配器**

```bash
git add src/nasmove/smb tests/fixtures/fake_smb.py tests/unit/smb tests/integration/test_smb_gateway.py
git commit -m "feat: implement reliable SMB gateway"
```

---

### Task 9：实现持久化检查点写入器

**Files:**
- Create: `src/nasmove/transfer/checkpoint_writer.py`
- Create: `tests/fixtures/transfer.py`
- Modify: `tests/conftest.py`
- Create: `tests/unit/transfer/test_checkpoint_writer.py`
- Create: `tests/fault/test_checkpoint_boundaries.py`

**Interfaces:**
- Consumes: `TaskRepository`、`LocalFileGateway`、`SmbGateway`、`TransferItemRecord`。
- Produces: `CheckpointWriter.copy(item: TransferItemRecord, start_offset: int, session: SessionInfo) -> CopyResult`。

- [ ] **Step 1：写 64 MiB 检查点顺序测试**

```python
from nasmove.transfer.checkpoint_writer import CHECKPOINT_BYTES, IO_BLOCK_BYTES, CheckpointWriter


def test_flush_precedes_checkpoint_commit(fake_dependencies) -> None:
    writer = CheckpointWriter(**fake_dependencies.as_kwargs())
    result = writer.copy(
        item=fake_dependencies.item(size=CHECKPOINT_BYTES + IO_BLOCK_BYTES),
        start_offset=0,
        session=fake_dependencies.session(generation=3),
    )
    assert result.bytes_copied == CHECKPOINT_BYTES + IO_BLOCK_BYTES
    assert fake_dependencies.trace.index("remote.flush@67108864") < fake_dependencies.trace.index(
        "repository.save_checkpoint@67108864"
    )
```

- [ ] **Step 2：运行测试并确认失败**

Run: `python3.12 -m pytest tests/unit/transfer/test_checkpoint_writer.py -v`

Expected: FAIL，错误包含缺少 `nasmove.transfer.checkpoint_writer`。

- [ ] **Step 3：实现复制循环**

定义 `IO_BLOCK_BYTES = 4 * 1024 * 1024` 和 `CHECKPOINT_BYTES = 64 * 1024 * 1024`。每次本地读取返回空字节时结束；每个非空块调用 `write_all()`；达到检查点时计算末尾最多 4 MiB 的本地窗口摘要，远端 `flush()`，查询远端长度，再调用 `repository.save_checkpoint()`。

- [ ] **Step 4：实现暂停与中断边界**

每个 4 MiB 块完成后检查 `CancellationToken`。暂停请求先完成当前块、刷新并保存检查点，然后返回 `CopyOutcome.PAUSED`。网络异常不保存未经刷新确认的偏移，返回 `CopyOutcome.INTERRUPTED` 和最后持久化检查点。

- [ ] **Step 5：运行边界故障测试**

Run: `python3.12 -m pytest tests/unit/transfer/test_checkpoint_writer.py tests/fault/test_checkpoint_boundaries.py -v`

Expected: 在写入前、短写中、刷新前、刷新后和数据库提交前注入故障时，恢复偏移从不超过经过刷新并提交的检查点。

- [ ] **Step 6：提交检查点写入器**

```bash
git add src/nasmove/transfer/checkpoint_writer.py tests/conftest.py tests/fixtures/transfer.py tests/unit/transfer/test_checkpoint_writer.py tests/fault/test_checkpoint_boundaries.py
git commit -m "feat: persist durable transfer checkpoints"
```

---

### Task 10：实现重试策略与断点恢复协调器

**Files:**
- Create: `src/nasmove/core/retry.py`
- Create: `src/nasmove/transfer/recovery.py`
- Modify: `tests/fixtures/transfer.py`
- Create: `tests/unit/core/test_retry.py`
- Create: `tests/unit/transfer/test_recovery.py`

**Interfaces:**
- Consumes: `TaskRepository`、`LocalFileGateway`、`SmbGateway`、`Checkpoint`。
- Produces: `RetryPolicy.delay_seconds(attempt: int, jitter: float) -> float | None`、`RecoveryCoordinator.find_safe_offset(item_id: TransferItemId) -> RecoveryDecision`。

- [ ] **Step 1：写退避序列测试**

```python
from nasmove.core.retry import RetryPolicy


def test_retry_schedule_without_jitter() -> None:
    policy = RetryPolicy()
    assert [policy.delay_seconds(index, jitter=0.0) for index in range(1, 9)] == [
        1.0,
        2.0,
        4.0,
        8.0,
        16.0,
        30.0,
        60.0,
        60.0,
    ]
    assert policy.delay_seconds(9, jitter=0.0) is None
```

- [ ] **Step 2：写安全偏移回退测试**

```python
def test_recovery_falls_back_until_window_hash_matches(recovery_fixture) -> None:
    recovery_fixture.remote_size = 128 * 1024 * 1024
    recovery_fixture.set_window_match(offset=128 * 1024 * 1024, matches=False)
    recovery_fixture.set_window_match(offset=64 * 1024 * 1024, matches=True)
    decision = recovery_fixture.coordinator.find_safe_offset(recovery_fixture.item_id)
    assert decision.safe_offset == 64 * 1024 * 1024
    assert decision.truncate_remote is True
```

- [ ] **Step 3：运行测试并确认失败**

Run: `python3.12 -m pytest tests/unit/core/test_retry.py tests/unit/transfer/test_recovery.py -v`

Expected: FAIL，错误包含缺少 `RetryPolicy` 或 `RecoveryCoordinator`。

- [ ] **Step 4：实现退避和错误分流**

`RetryPolicy` 使用固定基础序列并应用正负 20％抖动。认证、权限、路径、空间和协议错误直接返回不可重试；第 8 次快速重试后返回 `WAITING_FOR_NETWORK`，轻量探测周期固定 60 秒。

- [ ] **Step 5：实现三方断点协调**

`find_safe_offset()` 按检查点倒序处理，只考虑不大于远端实际长度的偏移；重新读取本地与远端确认窗口并比较 SHA-256。找到匹配点后截断远端超出部分；全部不匹配时隔离旧临时文件并返回从零开始。源指纹变化时返回 `RecoveryDisposition.SOURCE_CHANGED`。

- [ ] **Step 6：运行恢复测试**

Run: `python3.12 -m pytest tests/unit/core/test_retry.py tests/unit/transfer/test_recovery.py -v`

Expected: 覆盖远端较短、远端较长、窗口损坏、临时文件缺失、最终文件存在和源变化。

- [ ] **Step 7：提交重试与恢复**

```bash
git add src/nasmove/core/retry.py src/nasmove/transfer/recovery.py tests/fixtures/transfer.py tests/unit/core/test_retry.py tests/unit/transfer/test_recovery.py
git commit -m "feat: reconcile and resume interrupted transfers"
```

---

### Task 11：实现完整校验与原子提交

**Files:**
- Create: `src/nasmove/transfer/verification.py`
- Create: `src/nasmove/transfer/commit.py`
- Modify: `tests/fixtures/transfer.py`
- Create: `tests/unit/transfer/test_verification.py`
- Create: `tests/unit/transfer/test_commit.py`
- Create: `tests/fault/test_commit_crash_windows.py`

**Interfaces:**
- Consumes: `LocalFileGateway`、`SmbGateway`、`TaskRepository`、`SourceFingerprint`。
- Produces: `IntegrityVerifier.verify_full(item: TransferItemRecord) -> VerificationResult`、`TargetCommitter.commit(item: TransferItemRecord, verification: VerificationResult) -> CommitResult`。

- [ ] **Step 1：写完整回读校验测试**

```python
def test_full_verification_hashes_source_and_remote_from_zero(verification_fixture) -> None:
    verification_fixture.source_bytes = b"verified payload"
    verification_fixture.remote_bytes = b"verified payload"
    result = verification_fixture.verifier.verify_full(verification_fixture.item)
    assert result.matches is True
    assert result.source_bytes == len(b"verified payload")
    assert result.remote_bytes == len(b"verified payload")
    assert verification_fixture.remote_read_started_at == 0
```

- [ ] **Step 2：写竞态自动重命名测试**

```python
def test_commit_allocates_next_name_when_planned_name_was_taken(commit_fixture) -> None:
    commit_fixture.occupy("movie.mov")
    result = commit_fixture.committer.commit(commit_fixture.item, commit_fixture.valid_verification())
    assert str(result.final_path).endswith("movie (1).mov")
    assert commit_fixture.repository.get_item(commit_fixture.item.id).final_path == result.final_path
```

- [ ] **Step 3：运行测试并确认失败**

Run: `python3.12 -m pytest tests/unit/transfer/test_verification.py tests/unit/transfer/test_commit.py -v`

Expected: FAIL，错误包含缺少校验或提交模块。

- [ ] **Step 4：实现完整 SHA-256 校验**

关闭写句柄后分别新开本地源和远端临时文件读句柄，从偏移 0 读取到 EOF。摘要和精确长度同时匹配才返回 `matches=True`。源文件在校验前后指纹不同则返回 `source_unchanged=False`。

- [ ] **Step 5：实现同目录独占提交**

`TargetCommitter` 只接受 `matches=True` 且 `source_unchanged=True` 的结果。提交前查询最终名；存在时调用 `allocate_name()` 并先持久化新名称。随后调用 `rename_exclusive()`，重新查询最终文件大小和文件标识，再将文件项迁移到 `COMMITTED`。

- [ ] **Step 6：验证提交崩溃窗口**

Run: `python3.12 -m pytest tests/fault/test_commit_crash_windows.py -v`

Expected: 重命名前崩溃只留下 `.part`；重命名后、数据库提交前崩溃由恢复协调器发现最终文件并要求完整重校验；两种情况均不授权删除源文件。

- [ ] **Step 7：提交校验与提交模块**

```bash
git add src/nasmove/transfer/verification.py src/nasmove/transfer/commit.py tests/fixtures/transfer.py tests/unit/transfer/test_verification.py tests/unit/transfer/test_commit.py tests/fault/test_commit_crash_windows.py
git commit -m "feat: verify and atomically publish NAS files"
```

---

### Task 12：实现安全源删除与幂等恢复

**Files:**
- Create: `src/nasmove/transfer/deletion.py`
- Modify: `tests/fixtures/transfer.py`
- Create: `tests/unit/transfer/test_deletion.py`
- Create: `tests/fault/test_deletion_crash_windows.py`

**Interfaces:**
- Consumes: `authorize_source_delete()`、`LocalFileGateway`、`SmbGateway`、`TaskRepository`、`IntegrityVerifier`。
- Produces: `SourceDeletionService.delete_verified_source(item_id: TransferItemId, session: SessionInfo) -> DeletionResult`。

- [ ] **Step 1：写未验证目标禁止删除测试**

```python
import pytest

from nasmove.core.errors import UnsafeSourceDeletion


def test_source_is_not_removed_when_full_verification_is_missing(deletion_fixture) -> None:
    deletion_fixture.replace_item(full_hash_verified=False)
    with pytest.raises(UnsafeSourceDeletion):
        deletion_fixture.service.delete_verified_source(
            deletion_fixture.item.id,
            deletion_fixture.session,
        )
    assert deletion_fixture.local.remove_calls == []
```

- [ ] **Step 2：写会话变化强制重校验测试**

```python
def test_new_session_reverifies_target_before_delete(deletion_fixture) -> None:
    deletion_fixture.replace_item(verified_session_generation=2)
    deletion_fixture.replace_session(generation=3)
    deletion_fixture.service.delete_verified_source(
        deletion_fixture.item.id,
        deletion_fixture.session,
    )
    assert deletion_fixture.verifier.full_verify_calls == 1
```

- [ ] **Step 3：运行测试并确认失败**

Run: `python3.12 -m pytest tests/unit/transfer/test_deletion.py -v`

Expected: FAIL，错误包含缺少 `SourceDeletionService`。

- [ ] **Step 4：实现删除授权与文件删除**

服务重新读取任务项、源指纹、最终目标属性和当前 session generation，必要时执行完整重校验。只有 `authorize_source_delete()` 返回授权后才调用 `remove_file()`。删除成功后确认源路径不存在，再迁移为 `DONE`。

- [ ] **Step 5：实现目录自底向上清理**

仅处理任务规划时记录的源目录。每个目录删除前重新列出本地内容，非空则保留并记录警告；调用 `remove_empty_dir()`，不得调用递归删除。

- [ ] **Step 6：运行删除崩溃窗口测试**

Run: `python3.12 -m pytest tests/unit/transfer/test_deletion.py tests/fault/test_deletion_crash_windows.py -v`

Expected: 删除前崩溃恢复后重建授权；删除后、状态提交前崩溃识别源已不存在且目标摘要匹配，并幂等标记完成；删除失败时状态为 `SOURCE_RETAINED`。

- [ ] **Step 7：提交安全删除**

```bash
git add src/nasmove/transfer/deletion.py tests/fixtures/transfer.py tests/unit/transfer/test_deletion.py tests/fault/test_deletion_crash_windows.py
git commit -m "feat: delete sources only after verified commit"
```

---

### Task 13：组装传输引擎、队列和进度模型

**Files:**
- Create: `src/nasmove/transfer/transfer_engine.py`
- Create: `src/nasmove/transfer/progress.py`
- Modify: `tests/fixtures/transfer.py`
- Create: `tests/unit/transfer/test_transfer_engine.py`
- Create: `tests/unit/transfer/test_progress.py`
- Create: `tests/unit/transfer/test_queue.py`

**Interfaces:**
- Consumes: Tasks 5 至 12 的仓储、网关、写入、恢复、校验、提交和删除服务。
- Produces: `TransferEngine.run_task(task_id: TaskId, token: CancellationToken) -> TaskResult`、`QueueCoordinator.run_next() -> TaskResult | None`、`ProgressTracker.snapshot() -> ProgressSnapshot`。

- [ ] **Step 1：写端到端状态顺序测试**

```python
def test_move_item_follows_copy_verify_commit_delete_order(engine_fixture) -> None:
    result = engine_fixture.engine.run_task(engine_fixture.move_task.id, engine_fixture.token)
    assert result.success is True
    assert engine_fixture.trace == [
        "transfer",
        "full_verify",
        "commit",
        "authorize_delete",
        "delete_source",
    ]
```

- [ ] **Step 2：写串行队列测试**

```python
def test_queue_never_runs_two_tasks_at_once(queue_fixture) -> None:
    queue_fixture.enqueue("task-a")
    queue_fixture.enqueue("task-b")
    queue_fixture.run_until_empty()
    assert queue_fixture.max_concurrent_tasks == 1
    assert queue_fixture.completed_order == ["task-a", "task-b"]
```

- [ ] **Step 3：运行测试并确认失败**

Run: `python3.12 -m pytest tests/unit/transfer/test_transfer_engine.py tests/unit/transfer/test_queue.py -v`

Expected: FAIL，错误包含缺少传输引擎或队列协调器。

- [ ] **Step 4：实现传输编排**

`run_task()` 对每个文件严格执行：恢复决策、复制、完整校验、提交，以及移动任务的安全删除。单项错误按类别决定重试、等待网络、跳过或任务失败；任何异常捕获后先持久化安全状态，再发布 UI 事件。

- [ ] **Step 5：实现进度计算**

完整校验任务的总工作量为复制字节加远端回读字节。速度使用最近 30 秒样本的滑动窗口；样本少于 3 个时 ETA 为 `None`。进度事件最多每 250 毫秒发布一次，数据库累计值最多每秒合并一次。

- [ ] **Step 6：运行引擎与进度测试**

Run: `python3.12 -m pytest tests/unit/transfer -v`

Expected: 复制、移动、暂停、等待网络、校验失败、提交冲突和源删除失败路径全部通过。

- [ ] **Step 7：提交传输编排**

```bash
git add src/nasmove/transfer/transfer_engine.py src/nasmove/transfer/progress.py tests/fixtures/transfer.py tests/unit/transfer
git commit -m "feat: orchestrate serial reliable transfers"
```

---

### Task 14：实现应用启动恢复与生命周期

**Files:**
- Modify: `src/nasmove/transfer/recovery.py`
- Create: `src/nasmove/app.py`
- Create: `tests/fixtures/application.py`
- Modify: `tests/conftest.py`
- Create: `tests/unit/test_app_recovery.py`
- Create: `tests/fault/test_forced_quit_recovery.py`

**Interfaces:**
- Consumes: `TaskRepository.list_incomplete_tasks()`、`RecoveryCoordinator`、`QueueCoordinator`。
- Produces: `ApplicationService.start() -> StartupReport`、`ApplicationService.request_shutdown() -> ShutdownResult`。

- [ ] **Step 1：写启动恢复测试**

```python
from nasmove.core.states import TaskState


def test_running_tasks_become_interrupted_before_recovery(app_fixture) -> None:
    app_fixture.seed_running_task()
    report = app_fixture.service.start()
    assert report.interrupted_tasks == 1
    assert app_fixture.repository.task_state(app_fixture.task_id) is TaskState.INTERRUPTED
    assert app_fixture.trace[0] == "mark_interrupted"
```

- [ ] **Step 2：运行测试并确认失败**

Run: `python3.12 -m pytest tests/unit/test_app_recovery.py -v`

Expected: FAIL，错误包含缺少 `ApplicationService`。

- [ ] **Step 3：实现单实例与启动恢复**

应用启动时先获取 `~/Library/Application Support/NasMove/app.lock`，失败则通知已有实例并退出。打开数据库后执行 `PRAGMA integrity_check`，把遗留运行状态标记为 `INTERRUPTED`，加载未完成任务，并在缺失 Keychain 凭据时暂停相关任务。

- [ ] **Step 4：实现正常退出协议**

`request_shutdown()` 停止接收新任务，等待当前 4 MiB 块完成，刷新并保存检查点，将活动任务置为 `PAUSED`，关闭 SMB 会话、数据库和单实例锁。总等待超过 30 秒时向 UI 返回“仍在安全暂停中”，不强制杀死工作线程。

- [ ] **Step 5：运行强制退出恢复测试**

Run: `python3.12 -m pytest tests/unit/test_app_recovery.py tests/fault/test_forced_quit_recovery.py -v`

Expected: 在写块、刷新、校验、重命名和删除窗口结束进程后，重启均进入可解释状态且不误删源文件。

- [ ] **Step 6：提交生命周期服务**

```bash
git add src/nasmove/app.py src/nasmove/transfer/recovery.py tests/conftest.py tests/fixtures/application.py tests/unit/test_app_recovery.py tests/fault/test_forced_quit_recovery.py
git commit -m "feat: recover tasks across app restarts"
```

---

### Task 15：实现连接、来源和目标 UI

**Files:**
- Create: `src/nasmove/ui/view_models.py`
- Create: `src/nasmove/ui/connection_page.py`
- Create: `src/nasmove/ui/source_page.py`
- Create: `src/nasmove/ui/target_page.py`
- Create: `src/nasmove/ui/worker.py`
- Create: `tests/fixtures/ui.py`
- Modify: `tests/conftest.py`
- Create: `tests/unit/ui/test_connection_page.py`
- Create: `tests/unit/ui/test_source_page.py`
- Create: `tests/unit/ui/test_target_page.py`
- Create: `tests/unit/ui/test_worker_threading.py`

**Interfaces:**
- Consumes: 地址解析、Keychain、`SmbGateway`、`TaskPlanner`、`EventSink`。
- Produces: `ConnectionPage`、`SourcePage`、`TargetPage`、`BackgroundCommandWorker`。

- [ ] **Step 1：写连接页默认安全设置测试**

```python
from nasmove.ui.connection_page import ConnectionPage


def test_connection_page_defaults_to_port_445_keychain_and_encryption(qtbot) -> None:
    page = ConnectionPage()
    qtbot.addWidget(page)
    assert page.port_spinbox.value() == 445
    assert page.remember_password_checkbox.isChecked() is True
    assert page.require_encryption_checkbox.isChecked() is True
```

- [ ] **Step 2：写 UI 线程隔离测试**

```python
def test_connection_test_runs_outside_ui_thread(qtbot, ui_fixture) -> None:
    ui_fixture.connection_page.test_button.click()
    qtbot.waitUntil(lambda: ui_fixture.fake_service.called)
    assert ui_fixture.fake_service.thread_id != ui_fixture.qt_application.thread().currentThreadId()
```

- [ ] **Step 3：运行测试并确认失败**

Run: `QT_QPA_PLATFORM=offscreen python3.12 -m pytest tests/unit/ui/test_connection_page.py tests/unit/ui/test_worker_threading.py -v`

Expected: FAIL，错误包含缺少 UI 页面。

- [ ] **Step 4：实现连接页**

字段固定为配置名、地址、端口、共享名、用户名、域、密码、保存到 Keychain、要求加密。测试结果分为地址解析、TCP、SMB 协商、认证和共享访问；技术错误码置于可展开区域。

- [ ] **Step 5：实现来源页和目标页**

来源页支持多文件、多目录和拖放，层级保留固定开启，默认复制和完整校验；移动时校验控件强制开启。目标页通过 SMB 网关异步浏览目录，显示可用空间、安全余量和能力探测结果；路径输入必须先调用 `normalize_remote_path()`。

- [ ] **Step 6：运行 UI 单元测试**

Run: `QT_QPA_PLATFORM=offscreen python3.12 -m pytest tests/unit/ui/test_connection_page.py tests/unit/ui/test_source_page.py tests/unit/ui/test_target_page.py tests/unit/ui/test_worker_threading.py -v`

Expected: 页面默认值、校验锁定、路径错误、后台线程和取消扫描行为全部通过。

- [ ] **Step 7：提交任务创建 UI**

```bash
git add src/nasmove/ui/view_models.py src/nasmove/ui/connection_page.py src/nasmove/ui/source_page.py src/nasmove/ui/target_page.py src/nasmove/ui/worker.py tests/conftest.py tests/fixtures/ui.py tests/unit/ui
git commit -m "feat: add secure transfer setup UI"
```

---

### Task 16：实现队列、进度和结果 UI

**Files:**
- Create: `src/nasmove/ui/task_page.py`
- Create: `src/nasmove/ui/main_window.py`
- Modify: `src/nasmove/app.py`
- Create: `tests/unit/ui/test_task_page.py`
- Create: `tests/unit/ui/test_main_window.py`
- Create: `tests/unit/ui/test_results.py`
- Modify: `tests/fixtures/builders.py`

**Interfaces:**
- Consumes: `QueueCoordinator`、`ProgressSnapshot`、`TransferEvent`、`TaskResult`。
- Produces: `TaskPage`、`MainWindow`、可复制和可导出的脱敏结果报告。

- [ ] **Step 1：写复制与校验分离显示测试**

```python
from nasmove.ui.task_page import TaskPage
from tests.fixtures.builders import snapshot


def test_copy_complete_does_not_show_task_complete_while_verifying(qtbot) -> None:
    page = TaskPage()
    qtbot.addWidget(page)
    page.apply_snapshot(snapshot(copy_percent=100, verify_percent=25, state="verifying"))
    assert page.copy_progress.value() == 100
    assert page.verify_progress.value() == 25
    assert page.status_label.text() == "正在校验"
```

- [ ] **Step 2：写源保留结果测试**

```python
from nasmove.ui.task_page import TaskPage
from tests.fixtures.builders import result_with_source_retained


def test_delete_failure_is_reported_as_completed_with_warning(qtbot) -> None:
    page = TaskPage()
    qtbot.addWidget(page)
    page.show_result(result_with_source_retained("movie.mov"))
    assert "迁移完成，但源文件仍保留" in page.result_summary.text()
```

- [ ] **Step 3：运行测试并确认失败**

Run: `QT_QPA_PLATFORM=offscreen python3.12 -m pytest tests/unit/ui/test_task_page.py tests/unit/ui/test_results.py -v`

Expected: FAIL，错误包含缺少 `TaskPage`。

- [ ] **Step 4：实现任务队列页面**

页面显示当前文件、复制进度、校验进度、30 秒滑动速度、ETA、重试次数、倒计时和最近错误。等待任务支持重排和取消；当前任务支持暂停、继续和安全取消。取消对话框默认选择“保留断点”。

- [ ] **Step 5：实现结果报告**

结果按成功、移动完成、源保留、自动重命名、跳过、校验失败和传输失败分组。默认导出只包含文件名、路径哈希、错误类别和系统错误码；“包含完整路径”需要用户显式勾选。

- [ ] **Step 6：组装主窗口和应用入口**

`MainWindow` 使用向导式任务创建区和常驻队列区。`app.py` 创建 `QApplication`、依赖组合根、后台 worker 和主窗口；组合根是唯一可以同时导入具体 SQLite、Keychain、SMB 和 UI 实现的位置。

- [ ] **Step 7：运行完整 UI 测试**

Run: `QT_QPA_PLATFORM=offscreen python3.12 -m pytest tests/unit/ui -v`

Expected: 任务创建、队列重排、暂停、重连显示、结果分类和报告脱敏全部通过。

- [ ] **Step 8：提交任务 UI**

```bash
git add src/nasmove/ui/task_page.py src/nasmove/ui/main_window.py src/nasmove/app.py tests/fixtures/builders.py tests/unit/ui
git commit -m "feat: present queue progress and transfer results"
```

---

### Task 17：执行真实端到端故障注入

**Files:**
- Create: `tests/fault/test_network_disconnect.py`
- Create: `tests/fault/test_nas_restart.py`
- Create: `tests/fault/test_source_mutation.py`
- Create: `tests/fault/test_space_and_permissions.py`
- Create: `tests/fault/test_target_race.py`
- Create: `tests/integration/test_large_file_transfer.py`
- Create: `tests/integration/test_many_small_files.py`
- Create: `tests/fixtures/synology.py`
- Create: `tests/fixtures/fault_proxy.py`
- Modify: `tests/conftest.py`
- Create: `docs/verification/synology-test-matrix.md`

**Interfaces:**
- Consumes: 完整应用服务、真实测试 NAS、受控 TCP 故障代理。
- Produces: 可审计的故障测试结果和发布门禁记录。

- [ ] **Step 1：建立故障代理控制接口**

```python
class FaultProxy:
    def disconnect(self) -> None:
        self._control("disconnect")

    def add_latency(self, milliseconds: int) -> None:
        self._control(f"latency:{milliseconds}")

    def restore(self) -> None:
        self._control("restore")
```

代理必须只连接专用测试共享，不允许指向生产目录。

- [ ] **Step 2：写随机断网验收测试**

```python
import pytest


@pytest.mark.synology
def test_50_gib_file_survives_ten_disconnects(synology_fixture) -> None:
    result = synology_fixture.transfer_with_disconnects(
        size_gib=50,
        disconnect_percentages=[5, 13, 21, 34, 42, 55, 63, 76, 84, 93],
    )
    assert result.source_sha256 == result.target_sha256
    assert result.source_exists is True
    assert result.max_replayed_bytes <= 68 * 1024 * 1024
```

- [ ] **Step 3：写杀进程恢复测试驱动**

测试驱动在 20 个确定性伪随机偏移结束应用进程，重新启动后等待任务恢复完成。每轮验证：源文件未被提前删除、半成品保持 `.part` 名称、最终 SHA-256 一致。

- [ ] **Step 4：执行 NAS 重启和权限／空间测试**

按测试矩阵分别在写入、刷新、校验、提交和删除前重启 NAS；在传输中耗尽配额、撤销写权限、撤销删除权限和创建同名目标。每次记录 DSM 版本、SMB 配置、错误类别、恢复偏移和最终状态。

- [ ] **Step 5：执行大量小文件测试**

Run: `NASMOVE_TEST_SYNOLOGY=1 python3.12 -m pytest tests/integration/test_many_small_files.py -v`

Expected: 100,000 个 1 KiB 至 64 KiB 文件完成规划与复制；UI 事件循环保持响应；进程峰值内存不因缓存全部文件内容而线性增长；随机抽取 1,000 个文件进行 SHA-256 复核。

- [ ] **Step 6：执行完整故障套件**

Run: `NASMOVE_TEST_SYNOLOGY=1 python3.12 -m pytest tests/fault tests/integration/test_large_file_transfer.py -v`

Expected: 全部通过；任何失败都阻止进入 Task 18，不以重跑掩盖不稳定失败。

- [ ] **Step 7：填写并审阅真实 NAS 验证记录**

`docs/verification/synology-test-matrix.md` 必须记录环境、每个故障场景的执行次数、通过次数、最大重复字节、最终摘要和失败分析。不得包含 NAS 密码、认证报文或可从公网访问的地址。

- [ ] **Step 8：提交故障验证资产**

```bash
git add tests/fault tests/integration tests/conftest.py tests/fixtures/synology.py tests/fixtures/fault_proxy.py docs/verification/synology-test-matrix.md
git commit -m "test: prove recovery under Synology failures"
```

---

### Task 18：打包、安全审计和发布门禁

**Files:**
- Create: `deployment/pysidedeploy.spec`
- Create: `deployment/entitlements.plist`
- Create: `scripts/build_macos_app.sh`
- Create: `tests/release/test_bundle.py`
- Create: `docs/verification/release-checklist.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: 完整应用、锁定依赖和真实群晖验证记录。
- Produces: 签名、公证的 universal macOS `.app` 和发布验收记录。

- [ ] **Step 1：写应用包验证测试**

```python
from pathlib import Path


def test_release_bundle_contains_no_plaintext_credentials() -> None:
    bundle = Path("dist/NasMove.app")
    assert bundle.exists()
    forbidden = [b"NAS password", b"password=", b"session_key="]
    for file_path in bundle.rglob("*"):
        if file_path.is_file() and file_path.stat().st_size <= 20 * 1024 * 1024:
            content = file_path.read_bytes()
            assert all(token not in content for token in forbidden)
```

- [ ] **Step 2：运行测试并确认失败**

Run: `python3.12 -m pytest tests/release/test_bundle.py -v`

Expected: FAIL，原因是 `dist/NasMove.app` 尚不存在。

- [ ] **Step 3：创建可重复打包脚本**

`scripts/build_macos_app.sh` 必须启用 `set -euo pipefail`，验证 Python 3.12、锁文件哈希和干净构建目录，然后调用 `pyside6-deploy`。脚本只删除明确的 `build/NasMove` 与 `dist/NasMove.app`，不得删除工作区根目录或用户目录。

- [ ] **Step 4：配置签名与 Keychain 身份稳定性**

应用 bundle ID 固定为 `com.nasmove.app`。使用 Developer ID Application 签名，公证后 stapling。升级测试在旧版本保存测试凭据，安装新版本后验证相同连接 UUID 能读取同一 Keychain 条目。

- [ ] **Step 5：运行完整发布门禁**

Run: `python3.12 -m pytest -v`

Run: `python3.12 -m ruff check src tests`

Run: `python3.12 -m mypy src`

Run: `bash scripts/build_macos_app.sh`

Run: `python3.12 -m pytest tests/release/test_bundle.py -v`

Run: `codesign --verify --deep --strict --verbose=2 dist/NasMove.app`

Run: `spctl --assess --type execute --verbose=4 dist/NasMove.app`

Expected: 所有命令退出码为 0；`codesign` 报告有效签名；`spctl` 报告 accepted。

- [ ] **Step 6：执行人工发布清单**

在 Apple Silicon 和 Intel Mac 上分别验证：首次启动、连接测试、Keychain 保存、50 GB 复制、移动删除门禁、暂停、强制退出恢复、NAS 重启恢复、日志脱敏、应用升级和卸载后远端 `.part` 保留策略。

- [ ] **Step 7：更新 README 的已实现能力**

只记录已通过发布门禁的能力、支持的 macOS 范围、安装方法、数据安全语义、已知限制和故障报告方式。不得宣称“永不中断”或未测试的 DSM 兼容性。

- [ ] **Step 8：提交发布配置**

```bash
git add deployment scripts/build_macos_app.sh tests/release docs/verification/release-checklist.md README.md
git commit -m "build: package and verify macOS release"
```

---

## 4．每个任务的统一完成门禁

每项任务只有在以下条件同时满足时才能进入下一项：

1. 先观察到新增测试因预期原因失败。
2. 最小实现使目标测试通过。
3. 当前任务相关测试全部通过。
4. `ruff` 和 `mypy` 对相关目录通过。
5. 评审确认没有绕过领域状态机的源删除路径。
6. 评审确认日志和异常不包含凭据。
7. 若存在 Git 仓库，提交仅包含当前任务文件。

## 5．最终需求覆盖映射

| 设计要求 | 实施任务 |
|---|---|
| IP、主机名和 `smb://` | Task 4、Task 15 |
| NAS 配置、测试和 Keychain | Task 3、Task 6、Task 15 |
| 多文件／目录与层级保留 | Task 4、Task 15 |
| NAS 目标浏览和路径填写 | Task 8、Task 15 |
| 大文件与大量文件 | Task 9、Task 13、Task 17 |
| 重连、断点续传和重试 | Task 9、Task 10、Task 14、Task 17 |
| 完整性校验 | Task 7、Task 11、Task 17 |
| 进度、速度、ETA 和结果 | Task 13、Task 16 |
| 校验后安全删除 | Task 2、Task 11、Task 12、Task 17 |
| 任务队列串行执行 | Task 13、Task 16 |
| 冲突、Unicode、权限和空间 | Task 4、Task 8、Task 17 |
| 崩溃和 NAS 重启恢复 | Task 10、Task 14、Task 17 |
| 安全连接与日志脱敏 | Task 6、Task 8、Task 18 |
| macOS 打包、签名和公证 | Task 18 |

## 6．实施停止条件

出现以下任一情况必须停止实施并请求用户重新确认方案：

- 真实群晖不支持可靠偏移写、刷新、截断或同目录独占重命名。
- `smbprotocol` 无法在目标 DSM 配置上稳定恢复会话。
- 实现需要把密码写入普通文件、命令行参数或日志。
- 实现需要允许未完整校验的移动任务删除源文件。
- 需要扩大到多任务并发、Finder 扩展、其他协议或数据库 schema 之外的系统服务。
- 打包方案无法保持稳定 bundle ID 和 Keychain 访问身份。

## 7．计划执行说明

执行者每次只处理一个 Task。每个 Task 完成后先展示测试证据和文件 diff，再进入下一 Task。Task 3 和 Task 17 是硬门槛：没有真实群晖验证结果，不得以模拟测试代替并继续宣称可靠传输已经完成。
