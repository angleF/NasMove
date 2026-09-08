# macOS 发布检查清单

## 当前结论

已生成可启动的 `dist/NasMove.app`，bundle ID 固定为 `com.nasmove.app`，依赖锁哈希、凭据扫描和 `codesign --verify --deep --strict` 可执行。当前机器没有 Developer ID Application 证书，且 Python／Qt 工具链为 `x86_64`，所以现有包仅用于本机开发验证，不满足正式外部分发条件。

## 自动门禁

| 门禁 | 当前结果 | 发布要求 |
| --- | --- | --- |
| Python 版本 | 通过：3.12 | 必须保持 3.12 |
| 依赖锁哈希 | 通过 | `requirements.lock` 变化后必须显式更新哈希并评审 |
| 全量 pytest | 通过：849 passed，11 skipped | 退出码 0 |
| Ruff | 通过 | 退出码 0 |
| mypy strict | 通过：44 个源码文件 | 退出码 0 |
| 应用包生成 | 通过 | `dist/NasMove.app` 存在 |
| bundle ID | 通过：`com.nasmove.app` | 不得变化，否则 Keychain 身份和升级路径需重新验证 |
| 实际凭据扫描 | 通过 | 构建产物不得包含测试环境中的真实密码值或凭据环境变量名 |
| 本机代码签名完整性 | 通过：ad-hoc | 正式发布必须使用 Developer ID Application |
| Gatekeeper | 阻塞：rejected | Developer ID 签名、公证、stapling 后必须 accepted |
| Intel 架构 | 通过：`x86_64` | Intel Mac 人工验收通过 |
| Apple Silicon 架构 | 阻塞：未构建 | 在 Apple Silicon 上构建并人工验收，或形成经验证的 universal 包 |
| 50 GiB／10 次断连 | 阻塞：未执行 | 必须通过 |
| 100,000 个小文件 | 阻塞：未执行 | 必须通过 |

最近一次验证日期：2026-09-08。真实 NAS 故障回归为 `36 passed, 1 skipped`；该 skip 是需要显式授权的 DSM 重启。本轮之前已单独完成真实 DSM 重启恢复验证，详见 Synology 验证矩阵。

## 人工验收

- 首次启动时数据目录权限为 `0700`，SQLite、锁和日志能够正常创建。
- 输入 IP、主机名和 `smb://` 地址分别完成连接测试。
- 保存密码后退出并升级应用，相同连接 UUID 能从 Keychain 读取凭据。
- 选择多个文件和目录，确认层级、Unicode 文件名和空目录保持正确。
- 复制任务中执行暂停、恢复、断网和强制退出；检查 `.part`、检查点和最终 SHA-256。
- 移动任务只有在目标完整校验后删除源；模拟删除失败时目标与源均保留并显示警告。
- 同名目标在规划后和提交瞬间分别出现时，原目标不被覆盖。
- NAS 重启、磁盘满、权限撤销和网络切换时，错误分类与用户提示可理解且不含凭据。
- 检查应用日志与导出摘要，不包含密码、认证报文和未脱敏路径。
- 在 Intel 与 Apple Silicon Mac 上分别验证安装、首次启动、升级和卸载。

## 正式发布步骤

1. 在 Keychain 中安装有效的 Developer ID Application 证书。
2. 设置 `NASMOVE_SIGNING_IDENTITY` 和已配置的 `NASMOVE_NOTARY_PROFILE`。
3. 在目标架构环境运行 `scripts/build_macos_app.sh`。
4. 运行全量测试、发布测试、`codesign --verify` 和 `spctl --assess`。
5. 完成 50 GiB、100,000 文件及双架构人工清单。
6. 只有所有阻塞项关闭后，才可把 `.app` 标记为正式发布候选。

## 回滚

- 保留上一版已签名、公证的安装包及其依赖锁哈希。
- 新版出现数据或传输故障时停止分发，不删除 SQLite 状态、Keychain 凭据或 NAS 上的 `.part`。
- 回退应用版本后使用相同 bundle ID 和连接 UUID 打开现有任务；先执行只读恢复检查，再允许继续传输。
