"""User-facing status and allowlisted error messages, shared by task views."""

STATE_TEXT = {
    "draft": "尚未开始", "preflight": "正在检查任务", "queued": "排队中",
    "running": "正在传输", "verifying": "正在校验", "committing": "正在确认目标文件",
    "deleting_source": "正在完成移动", "waiting_for_network": "等待网络",
    "interrupted": "需要核对并恢复", "paused": "已暂停", "failed": "任务失败",
    "completed": "已完成", "completed_with_warnings": "已完成，但有警告", "canceled": "已取消",
    "execution_stopped": "执行已停止",
}

ERROR_TEXT = {
    "permission_denied": "无法访问文件或写入目标目录。请检查源文件读取权限和 NAS 目录写入权限。",
    "disk_full": "存储空间不足。请检查本机与 NAS 可用空间，释放空间后再处理任务。",
    "quota_exceeded": "NAS 账号存储配额不足。请调整配额后再处理任务。",
    "authentication_failed": "NAS 登录失败。请检查账号和密码，更新连接信息后测试连接。",
    "account_locked": "NAS 账号已锁定。请联系 NAS 管理员解锁。",
    "path_not_found": "源文件或目标目录不存在。请检查文件位置与 NAS 目录。",
    "source_not_found": "源文件不存在，可能已被此前的任务处理，请勿重复提交。",
    "file_locked": "文件正在被其他程序使用。请关闭占用文件的程序后再处理。",
    "timeout": "连接超时。请检查网络与 NAS 是否在线。",
    "connection_reset": "NAS 连接中断。请检查网络与 NAS 是否在线。",
    "network_name_deleted": "NAS 共享连接已断开。请检查共享服务与网络。",
    "dns_failure": "无法解析 NAS 地址。请检查地址或使用 IP 连接。",
    "unexpected_error": "程序发生异常。执行已停止，请展开错误详情并导出报告以便排查。",
    "database_thread_error": "任务数据库线程访问异常。请保留报告并更新应用。",
}


# Allowlisted labels for the persisted source-deletion outcome. Only these
# fixed texts are user-visible; the stored summary may contain paths.
DELETION_OUTCOME_TEXT = {
    "source_deleted": "源文件已移入废纸篓",
    "source_already_done": "源文件此前已处理",
    "source_already_absent": "源文件已不在本机，目标文件已校验",
    "source_missing_after_move": "源文件已不在本机（移动曾报告失败），目标文件已校验",
    "source_retained_move_failed": "源文件保留：移入废纸篓失败",
    "source_retained_still_exists": "源文件保留：移入废纸篓后仍然存在",
    "deletion_refused": "源文件删除被安全检查拒绝，未做任何删除",
}


def safe_code(code: str) -> str:
    return code if code in ERROR_TEXT else "unexpected_error"


def size_text(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= 1024
    return "0 B"


def duration_text(seconds: float | None) -> str:
    if seconds is None:
        return "正在估算"
    value = max(0, int(seconds))
    if value >= 3600:
        return f"{value // 3600} 小时 {(value % 3600) // 60} 分"
    if value >= 60:
        return f"{value // 60} 分 {value % 60} 秒"
    return f"{value} 秒"
