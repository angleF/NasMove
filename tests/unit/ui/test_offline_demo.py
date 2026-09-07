from pathlib import Path

from nasmove.ui.offline_demo import build_offline_demo_window


def test_offline_demo_completes_ui_flow_without_nas(qtbot, tmp_path: Path) -> None:
    window = build_offline_demo_window()
    qtbot.addWidget(window)
    source = tmp_path / "demo.bin"
    source.write_bytes(b"demo")
    window.source_page.set_sources([source])
    window.target_page.path_lineedit.setText("offline-target")

    window.target_page.add_to_queue_button.click()

    qtbot.waitUntil(lambda: window.task_page.result_summary.text() == "迁移完成")
    assert "离线演示" in window.windowTitle()
    assert window.task_page.queue_list.count() == 1
    assert "[completed]" in window.task_page.queue_list.item(0).text()
    assert window.target_page.creation_status_label.text() == "任务已加入队列"
    assert window.task_page.copy_progress.value() == 100
    assert window.task_page.verify_progress.value() == 100
