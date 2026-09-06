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

提交 SHA：`a41367f3621dc07cd448388bd372dfd23c06ffbf`。

## 独立审查修复轮次 1／5

### RED／GREEN

针对审查列出的 13 项 Important 先补充回归测试；修复前安全定向测试为 `11 failed, 10 passed`。修复后：

```text
/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/python -m pytest tests/unit/security -q
21 passed

/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/ruff check src/nasmove/security tests/unit/security tests/fixtures/security.py tests/conftest.py
All checks passed!

/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/mypy src/nasmove
Success: no issues found in 18 source files
```

### 逐项处理证据

1. 认证头在通用键值正则前整体替换，覆盖大小写、多个 `Authorization` 头及 Bearer／Basic／NTLM。
2. 敏感键采用大小写无关的前缀／后缀匹配，覆盖 password、passwd、passphrase、token、secret、authorization_header、bearer、basic、ntlm_response、ticket、session_key_id、hash 等；嵌套 mapping／sequence 递归处理，仅允许四类结构化 ID／错误字段原样保留。
3. 过滤器始终清空 `exc_text` 和 `exc_info`，Formatter 不再接触原异常参数或 traceback。
4. 递归 sanitizer 使用最大深度、元素上限和 visited 集合；任何容器迭代、str／repr 失败都返回 `<redacted>`，循环对象测试通过。
5. configure_logging 将同一脱敏 filter 安装到现有 `nasmove` 及 `nasmove.*` handler，阻断向 root 传播，安全 handler 保持唯一；普通 FileHandler 回归测试确认不写明文。
6. 默认 keyring import 失败和所有后端异常统一返回 `UnsafeCredentialBackend` 安全摘要，不包含原异常文本。
7. 初始化只解析一次 backend，要求与官方 `keyring.backends.macOS.Keyring` 精确同类；后续只调用已固定实例方法，切换全局 backend 的竞态测试通过。
8. 后端异常先在 except 块内记录失败标志，块外再抛安全错误；`__cause__`／`__context__` 均不携带原密码异常。
9. SMB URL、UNC、本地绝对路径和带空格路径整体保护或替换，必要时吞掉后续文本；仅保留安全路径占位或文件名。
10. bytes 不再解码或输出 repr，统一使用 `<redacted-bytes>`，包含 UTF-16 bytes 的测试通过。
11. 日志目录逐级创建并复检 `0700`；既有非私有父目录拒绝且不修改其 mode。系统临时目录和标准 macOS 日志祖先作为平台目录例外。
12. macOS 通过参数列表调用 `/bin/ls -lde` 与 `/bin/chmod -N`，固定 `LC_ALL=C`、`shell=False`；既有非当前用户 user/group/everyone ACL 拒绝，新建目标清 ACL 后复检；非 macOS 跳过 ACL 命令。
13. 幂等 configure 每次复检目标 owner、regular、symlink、mode 和 ACL；外部将文件改为 `0644` 后再次调用会修复为 `0600`。

### 风险

- macOS ACL 输出格式依赖系统 `/bin/ls -lde`，升级系统时应复核 ACL 解析测试；非 macOS 明确不执行 ACL 命令。
- 过滤器对异常或恶意对象采用 fail-closed，极端日志对象可能只保留 `<redacted>`，这是安全优先的预期损失。
- 实现提交仍为 `a41367f3621dc07cd448388bd372dfd23c06ffbf`；本轮修复提交为 `5f72d79d9ef44c773cb02681857613d1f82fe19d`（`fix: close credential and logging leaks`）。

## 独立审查修复轮次 2／5

### RED／GREEN

针对最终复审的 4 项 Important 先补充回归测试；修复前安全定向测试为 `4 failed, 22 passed`。修复后：

```text
/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/python -m pytest tests/unit/security -q
26 passed

/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/ruff check src/nasmove/security tests/unit/security tests/fixtures/security.py tests/conftest.py
All checks passed!

/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/mypy src/nasmove
Success: no issues found in 18 source files
```

### 逐项处理证据

1. `task_id`、`item_id`、`error_category`、`error_code` 仅接受长度受限的安全 ASCII 标识，并仍执行完整文本 sanitizer；嵌套 mapping 与 `LogRecord` extra 中的非法值统一为 `<redacted-field>`。
2. 敏感键值、Authorization、Bearer、Basic、NTLM 认证头统一 fail-closed 到行尾，覆盖逗号、分号、空格和 Unicode，Formatter 输出不含秘密片段。
3. 路径处理覆盖任意 POSIX 绝对路径、`~` 路径、UNC／SMB 路径和带分隔符的相对路径；先保护 URL／认证，再整体路径替换，带空格路径不会截断泄露目录片段。
4. configure_logging 安装一次仅作用于 `nasmove` 命名空间的全局 LogRecordFactory，保存并复用原 factory；配置后动态创建 `nasmove.*` logger 即使自带普通 handler 且 `propagate=False`，创建时记录也已完成脱敏，非 NasMove logger 不受影响。

### 风险

- 路径和认证信息按行 fail-closed 可能吞掉同一行后续普通诊断文本，这是避免残片泄露的有意取舍。
- LogRecordFactory 是进程级 logging 钩子，但只处理 `nasmove` 及其子命名空间；重复配置不会递归包装。
- 本轮修复提交为 `3a3f3f37738bef606b09459fb8dcc2c4a76dd816`（`fix: close remaining logging bypasses`）。

## 独立审查修复轮次 3／5

### RED／GREEN

新增动态子 logger 自有普通 FileHandler、`propagate=False` 且通过 `extra` 注入嵌套认证字段的回归测试；修复前该测试暴露 `POST_FACTORY_SECRET` 与嵌套认证值。修复后：

```text
/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/python -m pytest tests/unit/security -q
27 passed

/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/python -m pytest -q
590 passed, 1 skipped

/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/ruff check src/nasmove/security tests/unit/security tests/fixtures/security.py tests/conftest.py
All checks passed!

/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/mypy src/nasmove
Success: no issues found in 18 source files
```

### 处理证据

- `Logger.makeRecord` 包装器在标准实现完成 `extra` 写入后调用同一 `RedactingFilter`，因此动态 `nasmove.*` logger 的普通 handler 也只能接收清洗后的记录；非 NasMove logger 仍走原始路径。
- 全局 LogRecordFactory 继续负责 factory 阶段的消息、参数和异常清洗；原始 factory 链被保存并复用，配置通过锁保证幂等安装，避免递归包装。
- 回归覆盖普通 extra、嵌套 mapping 认证字段以及 formatter 实际输出，确认敏感值不落盘。

### 提交

本轮修复提交为 `ccd36a7097893e2dfaa5e5509dad627126c3c33c`（`fix: sanitize logging extra fields`）。

## 独立审查修复轮次 4／5

### RED／GREEN

新增 `logging.makeLogRecord` 兼容、post-factory 字典更新、转义引号凭据与标准 `/tmp` symlink ancestor 回归测试；修复前安全定向测试为 `4 failed, 27 passed`。修复后：

```text
/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/python -m pytest tests/unit/security -q
31 passed

/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/python -m pytest -q
594 passed, 1 skipped

/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/ruff check src/nasmove/security tests/unit/security tests/fixtures/security.py tests/conftest.py
All checks passed!

/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/mypy src/nasmove
Success: no issues found in 18 source files
```

### 逐项处理证据

1. factory 对 `record.name` 做 `str` 类型判断，`name=None` 的标准 `logging.makeLogRecord` 不再抛异常；新增 `makeLogRecord` wrapper 在 post-factory 字典更新后清洗 NasMove 记录，覆盖敏感 msg 与 nested extra。
2. 敏感键值正则改为整行匹配，转义双引号／单引号无法留下尾部 secret 或普通文本。
3. `/tmp` 与 `/var` 的已知系统 symlink ancestor 解析到固定目标并继续 ACL 检查；未知 symlink 仍拒绝，逐级目录安全校验保持不变。
4. LogRecordFactory 与 makeLogRecord wrapper 分别使用锁幂等安装，保留原始 factory／函数链；非 NasMove 记录走标准行为。

### 提交

本轮修复提交为 `5333ff8366de10d0f1bd447ef654ef35e7a42792`（`fix: harden logging compatibility edges`）。

## 最终独立复审

独立审查者对提交 `2061f1ab323c0c2e3ce8bb93c6d9bbebdfd8b100..e35037752303b3098e69738d36424e7c78f47e93` 复审结论为 `CLEAN`，未发现 Critical 或 Important 问题。复审确认：

- `logging.makeLogRecord()` 的 `name=None` 阶段安全兼容，且 post-factory 的 NasMove `msg`、普通 `extra` 与嵌套敏感字段均完成脱敏。
- 动态 `nasmove.*` 子 logger 自有 handler、`propagate=False` 场景安全；原始 LogRecordFactory／makeLogRecord 链保留，重复配置不会递归包装，非 NasMove logger 行为不变。
- 转义引号凭据整行 fail-closed；`/tmp`、`/var` 标准 macOS symlink ancestor 可用，未知 symlink 仍拒绝。
- Keychain backend、异常上下文、权限、ACL、bytes、路径及异常处理未发现新的高优先级问题。

最终验证：安全定向测试 `31 passed`；全量测试 `594 passed, 1 skipped`；Ruff 与 mypy 均通过。
