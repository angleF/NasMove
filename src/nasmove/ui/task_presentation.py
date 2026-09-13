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


def task_display_name(task: object) -> str:
    """Derive a recognizable, distinctive user-facing name for a task.

    If the task name is missing, generic, or identically matches the connection profile
    display name (a legacy behavior), fallback to the target directory name if present.
    """
    from pathlib import PurePosixPath

    raw_name = str(getattr(task, "name", "") or "").strip()
    connection = getattr(task, "connection", None)
    conn_display = str(getattr(connection, "display_name", "") or "").strip() if connection else ""
    target_root = getattr(task, "target_root", None)
    target_name = ""
    if target_root:
        target_str = str(target_root).strip().rstrip("/\\")
        if target_str:
            target_name = PurePosixPath(target_str).name

    is_generic = not raw_name or raw_name in {"NasMove 任务", "迁移任务"}
    is_legacy_connection_name = bool(conn_display and raw_name == conn_display)

    if (is_generic or is_legacy_connection_name) and target_name:
        return target_name
    return raw_name or target_name or "迁移任务"


def format_error_detail_report(
    code: str,
    *,
    task_summary: object | None = None,
    action: str = "move",
    copy_percent: int = 0,
    verify_percent: int = 0,
    total_files: int = 0,
    deletion_outcome: str | None = None,
) -> str:
    """Build a structured, reassuring diagnostic report for user display."""
    code = safe_code(code)
    total_items = int(getattr(task_summary, "total_items", 0) or total_files)
    committed_items = int(getattr(task_summary, "committed_items", 0))
    done_items = int(getattr(task_summary, "done_items", 0))
    total_bytes = int(getattr(task_summary, "total_bytes", 0))
    uncompleted = tuple(getattr(task_summary, "uncompleted_names", ()))
    all_committed = bool(
        getattr(task_summary, "all_committed", False)
        or (total_items > 0 and committed_items >= total_items)
        or (copy_percent >= 100 and verify_percent >= 100)
    )

    sections: list[str] = []

    # 1. 数据安全状态
    sections.append("【数据安全状态】")
    if all_committed:
        size_info = f"（共 {size_text(total_bytes)}）" if total_bytes > 0 else ""
        count_str = f"全部 {total_items} 个文件" if total_items > 0 else "所有文件"
        sections.append(f"• 目标 NAS 端：{count_str}{size_info}已 100% 完整写入并通过 SHA-256 校验，数据安全完整！")
        if action == "move":
            if done_items > 0:
                sections.append(f"• 本机源文件：{done_items} 个已成功移入废纸篓，剩余文件因被占用等原因保留在原路径。没有任何文件丢失。")
            else:
                sections.append("• 本机源文件：因被占用等原因未能移入废纸篓，仍完好保留在原路径。没有任何文件丢失。")
        else:
            sections.append("• 本机源文件：完好保留在本机，数据安全无损。")
    elif committed_items > 0:
        sections.append(f"• 目标 NAS 端：已成功写入 {committed_items}/{total_items} 个文件并通过校验；其余文件未完成。")
        sections.append("• 本机源文件：未完成文件的源文件完好保留在本机，没有任何数据丢失。")
    else:
        sections.append("• 目标 NAS 端：尚未写入任何文件，远端无残留半成品。")
        sections.append("• 本机源文件：所有源文件完好保留在本机，数据安全无损。")

    # 2. 受阻环节
    sections.append("\n【受阻环节】")
    if all_committed:
        if action == "move":
            sections.append("• 本地源文件移入废纸篓阶段（网络传输、数据校验及 NAS 原子提交均已顺利完成）。")
        else:
            sections.append("• 任务后处理阶段（数据复制与校验均已成功完成）。")
    elif verify_percent > 0 or copy_percent >= 100:
        sections.append("• 数据完整性校验或原子确认阶段。")
    elif copy_percent > 0:
        sections.append("• 数据复制与网络传输阶段。")
    else:
        sections.append("• 任务准备与 NAS 连接阶段。")

    # 3. 原因与错误代码
    sections.append("\n【原因与错误代码】")
    sections.append(f"• 错误代码：{code}")
    sections.append(f"• 原因分析：{ERROR_TEXT[code]}")
    if code == "file_locked":
        sections.append("• 常见占用原因：视频播放器正在播放、访达 (Finder) 预览、下载工具读写或外部进程占用。")
    if deletion_outcome and deletion_outcome in DELETION_OUTCOME_TEXT:
        sections.append(f"• 源文件清理记录：{DELETION_OUTCOME_TEXT[deletion_outcome]}")

    # 4. 受影响的文件
    if uncompleted:
        sections.append("\n【受影响的文件】")
        for name in uncompleted[:5]:
            status_hint = "（目标已写入，源文件待移入废纸篓）" if all_committed else "（未完成）"
            sections.append(f"• {name} {status_hint}")
        if total_items > len(uncompleted):
            sections.append(f"（提示：点击下方【查看文件结果】可核对全部 {total_items} 个文件的状态）")

    # 5. 处理建议
    sections.append("\n【处理建议】")
    if all_committed:
        sections.append("1. 目标 NAS 上的文件已完整就绪，无需重新传输。您可以点击下方【在 Finder 中查看目标目录】直接核验与使用。")
        if action == "move":
            sections.append("2. 若需清理本地剩余源文件：请关闭占用上述文件的程序后，点击右上角【重试】按钮，系统将直接完成废纸篓清理（仅需数秒）；或直接在 Finder 中手动移入废纸篓。")
        else:
            sections.append("2. 任务已完成复制，可直接使用 NAS 端文件。")
    elif committed_items > 0:
        sections.append("1. 已成功写入 NAS 的文件无需重新传输。")
        sections.append("2. 排除上述问题后点击右上角【重试】按钮，系统将自动从断点处继续传输未完成的文件。")
    else:
        sections.append("1. 请排查上述原因并检查网络、连接信息或可用存储空间。")
        sections.append("2. 排除故障后点击右上角【重试】按钮即可重新开始任务。")

    return "\n".join(sections)
