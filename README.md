# NasMove

NasMove 是面向 macOS 与群晖 NAS 的可靠 SMB 文件迁移桌面应用。它不承诺网络永不中断，而是通过远端临时对象、分块写入、持久化检查点、重连重试、完整 SHA-256 校验和原子提交，让中断可检测、可恢复。

“移动”严格采用“复制成功 → 完整校验 → 原子提交 → 再删除源文件”的语义。目标未确认完整时绝不删除源文件；删除失败时保留已复制目标和本地源，并显示警告。

## 已实现能力

- 支持 NAS IP、主机名和 `smb://` 输入，连接信息可编辑、测试并保存。
- 使用 SMB 3.x、签名和可选加密访问群晖共享；连接测试覆盖 TCP、SMB 协商、认证和共享访问。
- 支持多个本地文件／目录，固定保留目录层级，并保留空目录。
- 支持浏览或填写 NAS 目标目录；同名冲突自动保留扩展名并重命名。
- 队列可包含多个任务，但严格串行执行一个任务。
- 大文件按 4 MiB 块流式传输，每 64 MiB 建立经远端长度确认的持久化检查点。
- 网络或 SMB 会话中断后自动重连并指数退避重试；只从已确认安全偏移继续。
- 完整 SHA-256 校验通过后，以同目录独占重命名提交目标。
- 应用重启后从 SQLite 恢复未完成任务；数据库、锁文件和日志使用私有权限。
- 密码仅保存到 macOS Keychain，不写入 SQLite、普通配置或日志。
- UI 显示复制／校验进度、速度、ETA、重试和最终结果。

## 当前验证结论

真实 Synology 专用测试目录已验证：

- 基础 COPY、嵌套目录和空目录保留。
- 5 轮 128 MiB 传输，每轮在约 20％、50％、80％断开真实 SMB TCP，共 15 次断连，均自动重连并续传成功。
- 512 MiB 传输中真实重启 NAS，端口实际下线并恢复后自动续传成功。
- 源文件传输中变化、目标提交瞬间同名竞争、可用空间预检不足、传输中空间耗尽和权限撤销。
- 1,000 个小文件串行迁移及抽样 SHA-256 校验通过；峰值进程内存约 90 MiB。

以下高成本发布门禁尚未执行，不能视为已通过：

- 50 GiB 文件、10 次断连。
- 100,000 个小文件；按 1,000 文件实测速度估算约需 24 小时。
- Apple Silicon 原生包、Developer ID 签名、公证和 Gatekeeper 验收。

详见 [Synology 验证矩阵](docs/verification/synology-test-matrix.md) 和 [发布清单](docs/verification/release-checklist.md)。

## 技术栈

- Python 3.12
- PySide6
- `smbprotocol`／`smbclient`
- SQLite
- macOS Keychain（`keyring`）
- pytest、Ruff、mypy
- `pyside6-deploy`／Nuitka

## 本地开发

项目使用名为 `nasmove-py312` 的 Conda 环境：

```bash
conda activate nasmove-py312
python -m pip install -e '.[dev]'
nasmove
```

也可以直接运行：

```bash
python -m nasmove.ui.desktop_app
```

应用状态保存在：

```text
~/Library/Application Support/NasMove/
```

该目录会在确认不是符号链接且归当前用户所有后收紧到 `0700`。

## 验证

```bash
python -m pytest -q
python -m ruff check src tests
python -m mypy src
```

真实 NAS 测试仅允许指向 `NasMoveTest/<run-id>` 隔离目录。测试环境变量文件不得提交到仓库，且应设置为 `0600`。

```bash
set -a
source /Users/fuzhaoliang/Downloads/nasmove-test.env
set +a
python -m pytest tests/fault tests/integration -v
```

高成本门禁需额外显式启用：

```bash
NASMOVE_TEST_SYNOLOGY_LARGE=1 python -m pytest tests/integration/test_large_file_transfer.py -v
NASMOVE_TEST_SYNOLOGY_SCALE=1 python -m pytest tests/integration/test_many_small_files.py -v
NASMOVE_TEST_DSM_RESTART=1 python -m pytest tests/fault/test_nas_restart.py -v
```

## 构建 macOS 应用包

```bash
conda activate nasmove-py312
scripts/build_macos_app.sh
```

输出为 `dist/NasMove.app`。没有 `NASMOVE_SIGNING_IDENTITY` 时脚本使用 ad-hoc 签名，仅适合本机验证；正式分发必须提供 Developer ID Application 证书和 `NASMOVE_NOTARY_PROFILE`，完成公证及 stapling。

当前构建主机的 Python 和 Qt 为 `x86_64`，因此本机产物不是 universal app。要正式发布，需在 Apple Silicon 构建／验证对应架构，再合并或分别发布架构包。

## 安全边界

- 不在命令行参数、测试报告或日志中输出 NAS 密码。
- 不把 SMB 连接“永不中断”作为承诺。
- 不覆盖同名目标；提交前若发生竞争，自动分配新名称。
- 断点只认 SQLite 中已持久化且经远端长度与窗口哈希确认的位置。
- 对不确定的重命名结果、来源变化或目标身份变化采取失败关闭策略。
- 故障注入代理只允许监听本机回环地址。

## 设计与实施文档

- [设计文档](docs/superpowers/specs/2026-09-05-nas-reliable-transfer-design.md)
- [实施计划](docs/superpowers/plans/2026-09-05-nas-reliable-transfer.md)
- [Synology 验证矩阵](docs/verification/synology-test-matrix.md)
- [发布清单](docs/verification/release-checklist.md)
