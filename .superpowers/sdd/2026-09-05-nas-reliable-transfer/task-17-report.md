# Task 17 报告：真实 Synology 故障注入

## 状态

测试门禁与验证矩阵已建立；真实 NAS 故障注入尚未执行，不能视为通过。

## 已完成

- 增加 loopback-only `FaultProxy` 控制接口，拒绝非本机代理地址。
- 增加 Synology 环境变量门禁，要求专用共享、`NasMoveTest/<run-id>` 根目录和显式 ready 标志。
- 增加断网、NAS 重启、源变化、空间／权限、目标竞争、大文件和大量小文件测试入口。
- 增加脱敏验证矩阵，明确记录未执行状态；测试默认跳过，不把 skip 计为 pass。

## 当前验证

```text
Task 17 guarded tests: 7 skipped
ruff check src tests: All checks passed!
python -m mypy src/nasmove: Success: no issues found in 37 source files
```

真实执行仍需要用户提供专用测试 NAS、故障代理和测试凭据；没有这些条件不得启用 `NASMOVE_TEST_SYNOLOGY_READY=1`。
