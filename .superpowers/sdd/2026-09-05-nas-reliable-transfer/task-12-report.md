# Task 12 实施报告：安全源删除与幂等恢复

## 状态

DONE

## 实施摘要

- 新增 `SourceDeletionService` 与不可变 `DeletionResult`。服务每次删除前重新读取文件项、源指纹、最终目标属性和 SMB session generation。
- 目标必须存在、为普通文件、大小／对象 ID／SHA-256 与持久化提交记录一致；只有 `authorize_source_delete()` 成功后才进入 `SOURCE_DELETE_AUTHORIZED` 并调用本地删除。
- 会话代次变化时将验证器绑定到最终目标路径并执行完整重校验；源删除后重新确认源不存在，再迁移为 `DONE`。
- 删除异常保留源与 NAS 目标并迁移为 `SOURCE_RETAINED`。源已不存在且最终目标摘要匹配时，恢复路径幂等迁移为 `DONE`，覆盖删除后、状态提交前崩溃窗口。
- 目录清理只接受调用方传入的规划目录，按路径深度自底向上；删除前列出内容，非空目录保留并记录 warning，使用 `remove_empty_dir()`，不执行递归删除。
- 扩展传输夹具记录指纹、删除调用、删除失败和状态提交崩溃；新增删除单元测试与故障窗口测试。

## TDD 证据

测试先运行并确认 RED：

```text
ModuleNotFoundError: No module named 'nasmove.transfer.deletion'
```

最小实现后定向删除与故障测试：

```text
10 passed
```

## 验证

```text
python -m pytest -q
721 passed, 1 skipped in 61.41s
```

```text
ruff check src tests
All checks passed!
```

```text
python -m mypy src/nasmove
Success: no issues found in 26 source files
```

```text
git diff --check
通过
```

跳过项为需要显式设置 `NASMOVE_TEST_SMB=1` 的真实 SMB 集成测试。

## 独立复审修复

复审发现会话变化后的 verifier 结果允许缺失 `full_hash_verified`、`session_generation` 及内容绑定字段，存在旧版或伪造 verifier 绕过当前会话安全门槛的风险。新增回归测试先确认 RED：

```text
7 failed, 9 passed
```

修复后，重校验结果必须严格为布尔成功、完整校验成功且 generation 等于当前会话；source/remote SHA-256、精确长度、远端对象 ID 同持久化记录和本次目标回读摘要一致，所有字段缺失、类型错误或不一致均 fail-closed。

修复后的定向删除与故障测试：

```text
18 passed
```

本轮最终全量验证：

```text
729 passed, 1 skipped in 59.17s
```

Ruff、mypy 与 `git diff --check` 均通过。

## 独立复审修复轮 2

复审发现删除服务把 `get_task()` 视为可选，仓储无法读取任务时可能绕过 MOVE/COPY 判定。新增任务读取与 action 回归测试先确认 RED：

```text
5 failed, 17 passed
```

修复后 `TaskRepository.get_task()` 为强制协议；每次删除先读取任务，读取异常、任务缺失、字段缺失或类型错误、COPY 及其他 action 均转换为 `UnsafeSourceDeletion`，只有精确的 `TransferAction.MOVE` 才可继续。

本轮最终定向测试：

```text
24 passed
```

最终全量验证：

```text
735 passed, 1 skipped in 58.40s
```

Ruff、mypy 与 `git diff --check` 均通过。

## 风险与边界

- 删除服务依赖任务项持久化的 `revision` 与状态 CAS；删除后的 `DONE` 提交若崩溃，下一次调用通过源缺失和目标摘要匹配恢复。
- 远端目标校验与本地删除不是跨系统原子事务；服务保守处理目标缺失、属性变化、摘要不匹配和本地删除失败，不删除源文件。
- 目录清理默认不推断未记录的目录；只有明确规划目录会被尝试清理。

## 最终独立复审

结论：`CLEAN / ADDRESSED`。

- `TaskRepository.get_task()` 为强制协议，每次删除前必须读取任务并确认精确的 `TransferAction.MOVE`。
- 读取异常、任务缺失、action 缺失或类型错误、COPY 及其他动作均 fail-closed。
- 合法 MOVE、新会话完整重校验、目标摘要／长度／对象身份绑定均可通过。
- 删除后数据库提交崩溃可幂等恢复为 `DONE`；删除失败保留源文件和已验证目标。
- 目录清理仍只处理规划目录，按深度倒序、非空保留且不使用递归删除。

主控侧最终验证：`735 passed, 1 skipped in 60.57s`；Ruff、mypy 与 `git diff --check` 均通过。
