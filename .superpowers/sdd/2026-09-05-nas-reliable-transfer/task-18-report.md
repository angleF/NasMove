# Task 18 报告：macOS 打包、安全审计和发布门禁

## 状态

应用打包、安全扫描、启动验证和发布门禁自动化已实现。本机构建产物可用于开发验证，但正式外部分发仍被证书、公证、Apple Silicon 架构以及两个高成本容量门禁阻塞，因此不能标记为正式发布候选。

## 已完成

- 使用 Python 3.12、PySide6 Deploy 和 Nuitka 生成 `dist/NasMove.app`。
- bundle ID 固定为 `com.nasmove.app`，应用入口连接真实 SQLite、Keychain、SMB、串行队列、恢复与校验组件。
- 构建脚本校验依赖锁哈希，使用明确的构建／输出目录，并支持 Developer ID 签名、公证和 stapling 参数。
- 本机构建采用 ad-hoc 签名；`codesign --verify --deep --strict` 通过。
- 应用包离屏启动 6 秒保持运行，随后正常终止；运行时数据目录自动收紧为 `0700`。
- 发布测试检查应用结构、bundle ID，并扫描构建产物，确认不含测试凭据值或凭据环境变量名。
- 全量测试 `840 passed, 11 skipped`；Ruff 与 mypy strict 均通过。

## 当前构建限制

- 本机没有有效的 `Developer ID Application` 身份，无法完成正式签名和 Apple 公证；`spctl` 因此为 `rejected`。
- 当前 Python／Qt 工具链仅为 `x86_64`，产物不是 Apple Silicon 或 universal app。
- 50 GiB／10 次断连和 100,000 个小文件门禁尚未执行。
- 人工清单中的升级、跨架构安装与最终用户交互仍需在正式签名候选包上完成。

## 结论

Task 18 的工程实现和本机自动验证已经完成；正式发布验收未完成。关闭上述阻塞项前，不得把当前 `.app` 对外标记为正式版本。
