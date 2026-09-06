# Task 17 报告：真实 Synology 故障注入

## 状态

测试门禁与验证矩阵已建立；真实 NAS 故障注入尚未执行，不能视为通过。已完成一次真实 Synology 小文件复制闭环探针。

## 已完成

- 增加 loopback-only `FaultProxy` 控制接口，拒绝非本机代理地址。
- 增加 Synology 环境变量门禁，要求专用共享、`NasMoveTest/<run-id>` 根目录和显式 ready 标志。
- 增加断网、NAS 重启、源变化、空间／权限、目标竞争、大文件和大量小文件测试入口。
- 增加脱敏验证矩阵，明确记录未执行状态；测试默认跳过，不把 skip 计为 pass。
- 使用生产 `TaskPlanner`、`CheckpointWriter`、`RecoveryCoordinator`、`IntegrityVerifier` 和 `TargetCommitter` 完成 9.5 MB COPY 探针；写入、完整校验、原子提交和清理均成功，源文件仍保留。

## 当前验证

```text
SMB capability probe: 1 passed
真实 COPY 闭环探针: 1 passed（9.5 MB）
Task 17 guarded tests: 7 skipped；断网 harness 仍为未实现占位
ruff check src tests: All checks passed!
python -m mypy src/nasmove: Success: no issues found in 37 source files
```

真实故障注入仍需要 FaultProxy 数据通道、NAS 重启／配额／ACL 操作和完整 harness；当前结果不得外推为 50 GiB 或故障恢复通过。
