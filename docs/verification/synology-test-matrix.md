# Synology 故障验证矩阵

## 当前状态

已完成 SMB 能力探针和一次 9.5 MB 真实 COPY 闭环；故障注入场景仍未执行，禁止将跳过记为通过。

## 启用条件

- `NASMOVE_TEST_SYNOLOGY=1`
- `NASMOVE_TEST_SYNOLOGY_READY=1`
- `NASMOVE_SYNOLOGY_HOST`、`NASMOVE_SYNOLOGY_SHARE`
- `NASMOVE_SYNOLOGY_TEST_ROOT=NasMoveTest/<run-id>`
- `NASMOVE_FAULT_PROXY` 指向本机回环地址上的受控代理

测试根目录必须位于专用共享的 `NasMoveTest/` 下。密码、认证报文和公网地址不得写入记录。

## 待执行场景

| 场景 | 次数 | 通过 | 最大重复字节 | 最终状态 | 备注 |
| --- | ---: | ---: | ---: | --- | --- |
| 基础 COPY 闭环 | 1 | 1 | 0 | 通过 | 9.5 MB；完整校验、原子提交、清理通过；源文件保留 |
| 写入中断网 | 0 | 0 | — | 未执行 | 等待 FaultProxy |
| NAS 重启 | 0 | 0 | — | 未执行 | 需人工 DSM 操作 |
| 源文件变化 | 0 | 0 | — | 未执行 | 需专用源目录 |
| 空间／权限不足 | 0 | 0 | — | 未执行 | 需配额和 ACL 操作 |
| 目标同名竞争 | 0 | 0 | — | 未执行 | 需专用共享 |
| 大文件 | 0 | 0 | — | 未执行 | 不得伪造 50 GiB 结果 |
| 大量小文件 | 0 | 0 | — | 未执行 | 不得伪造 100,000 文件结果 |
