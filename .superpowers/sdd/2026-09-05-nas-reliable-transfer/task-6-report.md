# Task 6 实施报告：Keychain 与日志脱敏

## 状态

DONE

## TDD 证据

### RED

先新增安全测试与测试替身，再运行：

```text
/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/python -m pytest tests/unit/security -v
```

结果为收集失败：`UnsafeCredentialBackend` 尚未定义，且 `nasmove.security` 尚不存在。

### GREEN

完成最小实现后运行：

```text
/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/python -m pytest tests/unit/security -v
```

结果：`10 passed`。

## 实现摘要

- 新增 `UnsafeCredentialBackend(DomainValidationError)`，作为 Keychain 不安全或异常的安全错误边界。
- `MacOSKeychainCredentialStore` 初始化和每次读写删操作都检查实际 backend class module 必须为 `keyring.backends.macOS`；固定 service 为 `com.nasmove.smb`，account 为 `str(ConnectionProfileId)`，不提供文件回退或密码缓存。
- `RedactingFilter` 递归处理消息、参数、映射、序列、字节和异常摘要，脱敏敏感键、Bearer／Basic／NTLM、SMB URL／UNC 凭据；结构化额外字段仅保留允许字段，路径默认仅保留文件名。
- `configure_logging` 使用默认 `~/Library/Logs/NasMove/nasmove.log`，创建并复检 `0700` 日志目录和 `0600` 日志文件，拒绝 symlink／非普通目标，并保证重复配置不增加 handler。
- 测试 fixture 使用内存 fake keyring，不访问真实 Keychain。

## 文件

- `src/nasmove/core/errors.py`
- `src/nasmove/security/keychain_store.py`
- `src/nasmove/security/redacted_logging.py`
- `tests/conftest.py`
- `tests/fixtures/security.py`
- `tests/unit/security/test_keychain_store.py`
- `tests/unit/security/test_redacted_logging.py`

## 验证

```text
/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/python -m pytest tests/unit/security -q
10 passed

/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/python -m pytest -q
573 passed, 1 skipped

/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/ruff check src/nasmove/security tests/unit/security tests/conftest.py tests/fixtures/security.py
All checks passed!

/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/mypy src/nasmove
Success: no issues found in 18 source files
```

## 风险

- Python 字符串仍无法保证进程内绝对清零；实现只缩短密码驻留范围，不做明文缓存。
- 日志脱敏按默认策略保留路径文件名；如未来需要诊断完整路径，应另行显式授权并采用路径哈希或受控导出。
- backend module 门禁依赖 Python keyring macOS backend 的稳定模块标识；升级 keyring 时应复核并更新兼容测试。

## 提交

提交信息：`feat: secure credentials and redact diagnostics`

提交 SHA：待提交后填写。
