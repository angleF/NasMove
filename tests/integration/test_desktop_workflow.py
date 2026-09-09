"""Real Qt worker + SQLite + SMB path, confined to the configured test directory."""
import os
from uuid import uuid4

import pytest

from nasmove.core.model import RemotePath
from nasmove.ui.desktop_app import build_desktop_runtime
from nasmove.ui.offline_demo import InMemoryCredentialStore


@pytest.mark.skipif(os.environ.get("NASMOVE_TEST_SYNOLOGY") != "1", reason="requires isolated Synology test directory")
def test_desktop_creates_and_executes_real_copy(qtbot, tmp_path):
    root = os.environ["NASMOVE_SYNOLOGY_TEST_ROOT"].strip("/")
    assert root.startswith("NasMoveTest/")
    name = f"workbench-{uuid4().hex}.bin"
    source = tmp_path / name
    source.write_bytes(b"NasMove UI workflow\n" * 450000)
    runtime = build_desktop_runtime(data_dir=tmp_path / "state", credential_store=InMemoryCredentialStore())
    window = runtime.window
    qtbot.addWidget(window)
    assert runtime.application.start().started
    page = window.connection_page
    page.address_lineedit.setText(os.environ["NASMOVE_SYNOLOGY_HOST"])
    page.port_spinbox.setValue(int(os.environ.get("NASMOVE_SYNOLOGY_PORT", "445")))
    page.share_lineedit.setText(os.environ["NASMOVE_SYNOLOGY_SHARE"])
    page.username_lineedit.setText(os.environ.get("NASMOVE_TEST_SMB_USERNAME") or os.environ["NASMOVE_USER"])
    page.password_lineedit.setText(os.environ.get("NASMOVE_TEST_SMB_PASSWORD") or os.environ["NASMOVE_PASSWORD"])
    try:
        page.test_button.click()
        qtbot.waitUntil(lambda: page._thread is None, timeout=30000)
        qtbot.waitUntil(lambda: window.target_page._thread is None, timeout=30000)
        assert window.connection_status_label.text() == "NAS：连接测试成功"
        window.source_page.set_sources([source])
        window.source_page.move_checkbox.setChecked(False)
        window.target_page.set_selected_path(root)
        window.target_page.add_to_queue_button.click()
        qtbot.waitUntil(lambda: window.task_creation_controller._thread is None, timeout=30000)
        assert window.task_page.queue_list.count() == 1
        qtbot.waitUntil(lambda: not window.queue_execution.running, timeout=120000)
        assert window.task_page.status_label.text() == "已完成"
        assert runtime.repository.list_tasks()[0].state.value == "completed"
        assert source.exists()
        target = RemotePath(f"{root}/{name}")
        with runtime.gateway.open_read(target) as stream:
            assert stream.read() == source.read_bytes()
        runtime.gateway.remove_file(target)
    finally:
        runtime.queue.request_cancel()
        qtbot.waitUntil(lambda: not window.queue_execution.running, timeout=120000)
        runtime.application.request_shutdown()
