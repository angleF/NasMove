from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from nasmove.core.model import TaskId
from nasmove.core.states import TaskState
from nasmove.persistence.sqlite_repository import SqliteTaskRepository
from nasmove.transfer.transfer_engine import TransferEvent
from nasmove.ui.main_window import MainWindow
from nasmove.ui.task_controller import TaskController
from nasmove.ui.task_page import TaskPage
from tests.fixtures.builders import build_task_record


def test_real_repository_can_be_used_by_planning_and_queue_threads(tmp_path):
    tmp_path.chmod(0o700)
    with SqliteTaskRepository(tmp_path / "tasks.db") as repository:
        task = build_task_record()
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(repository.create_task, task, []).result()
            assert executor.submit(repository.get_task, task.id).result().id == task.id
        assert repository.get_task(task.id).id == task.id


def test_home_is_empty_workbench_with_new_task_action(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    assert window.pages.currentWidget() is window.task_page
    assert window.task_page.status_label.text() == "尚未创建任务"
    assert window.back_button.isHidden()
    window.new_task_button.click()
    assert window.pages.currentWidget() is window.connection_page


def test_background_event_does_not_overwrite_selected_task(qtbot):
    page = TaskPage()
    qtbot.addWidget(page)
    controller = TaskController(page)
    first = replace(build_task_record(), state=TaskState.QUEUED)
    second = replace(first, id=TaskId("second"), name="Second")
    controller.load_queue((first, second))
    page.queue_list.setCurrentRow(1)
    controller.publish(TransferEvent(first.id, TaskState.RUNNING))
    assert page.status_label.text() == "排队中"
    page.queue_list.setCurrentRow(0)
    assert page.status_label.text() == "正在传输"


def test_failure_displays_actionable_reason_without_secrets(qtbot):
    page = TaskPage()
    qtbot.addWidget(page)
    page.show_execution_error("permission_denied")
    assert "停止" in page.status_label.text()
    assert "权限" in page.result_summary.text()
    assert page.error_details.toPlainText()
    assert not page.pause_button.isEnabled()


def test_empty_production_queue_does_not_report_failure(qtbot, tmp_path):
    from nasmove.ui.desktop_app import build_desktop_runtime
    runtime = build_desktop_runtime(data_dir=tmp_path)
    qtbot.addWidget(runtime.window)
    runtime.window.queue_execution.start()
    qtbot.waitUntil(lambda: not runtime.window.queue_execution.running)
    assert runtime.window.task_page.status_label.text() == "尚未创建任务"
    runtime.repository.close()


def test_late_progress_does_not_undo_completed_result(qtbot):
    from nasmove.transfer.transfer_engine import TaskResult
    from tests.fixtures.builders import snapshot
    page = TaskPage()
    qtbot.addWidget(page)
    controller = TaskController(page)
    task = replace(build_task_record(), state=TaskState.QUEUED)
    controller.load_queue((task,))
    controller.publish(TransferEvent(task.id, TaskState.RUNNING))
    controller.publish_result(TaskResult(True, TaskState.COMPLETED, task_id=task.id))
    controller.publish_progress(snapshot(copy_percent=20))
    assert page.status_label.text() == "已完成"
    assert page.copy_progress.value() == 100


def test_error_history_is_available_after_reopening(qtbot, tmp_path):
    tmp_path.chmod(0o700)
    task = replace(build_task_record(), state=TaskState.QUEUED)
    with SqliteTaskRepository(tmp_path / "history.db") as repository:
        repository.create_task(task, [])
        repository.record_ui_error(task.id, "permission_denied")
    with SqliteTaskRepository(tmp_path / "history.db") as repository:
        page = TaskPage()
        qtbot.addWidget(page)
        controller = TaskController(page, repository=repository)
        controller.load_queue(tuple(repository.list_tasks()))
        assert "权限" in page.result_summary.text()


def test_copy_progress_is_only_reported_after_durable_checkpoint(fake_dependencies):
    from nasmove.transfer.checkpoint_writer import IO_BLOCK_BYTES, CheckpointWriter
    offsets = []
    def report(item, offset):
        if offset:
            assert fake_dependencies.repository.checkpoints[-1].confirmed_offset == offset
        offsets.append(offset)
    writer = CheckpointWriter(**fake_dependencies.as_kwargs(), progress=report)
    result = writer.copy(fake_dependencies.item(size=IO_BLOCK_BYTES + 7), 0, fake_dependencies.session(generation=1))
    assert offsets == [0, result.confirmed_offset]


def test_hashing_reports_each_block_without_changing_digest():
    from io import BytesIO

    from nasmove.localio.hashing import sha256_stream
    offsets = []
    result = sha256_stream(BytesIO(b"abcdefgh"), 3, progress=offsets.append)
    assert offsets == [3, 6, 8]
    assert result == sha256_stream(BytesIO(b"abcdefgh"))


def test_persisted_queue_order_wins_over_original_enqueue_order(tmp_path):
    from nasmove.transfer.transfer_engine import QueueCoordinator, TaskResult
    tmp_path.chmod(0o700)
    seen = []
    with SqliteTaskRepository(tmp_path / "queue.db") as repository:
        first = replace(build_task_record(), state=TaskState.QUEUED)
        second = replace(first, id=TaskId("second"))
        repository.create_task(first, [])
        repository.create_task(second, [])
        class Engine:
            def run_task(self, task_id, token):
                seen.append(task_id)
                return TaskResult(False, TaskState.PAUSED)
        queue = QueueCoordinator(Engine(), repository)
        queue.enqueue(first.id)
        queue.enqueue(second.id)
        repository.reorder_queued_tasks((second.id, first.id))
        queue.run_next()
        assert seen == [second.id]


def test_reconnected_task_clears_old_network_warning(qtbot):
    page = TaskPage()
    qtbot.addWidget(page)
    page.apply_event(TransferEvent(TaskId("one"), TaskState.WAITING_FOR_NETWORK,
        retry_attempt=2, retry_delay=10))
    assert "重连" in page.result_summary.text()
    page.apply_event(TransferEvent(TaskId("one"), TaskState.RUNNING))
    assert page.result_summary.text() == ""


def test_verifying_state_keeps_copy_and_full_readback_as_distinct_phases(qtbot):
    page = TaskPage()
    qtbot.addWidget(page)

    page.set_workspace_state(TaskState.VERIFYING)

    assert "复制完成" in page.phase_label.text()
    assert "完整回读校验中" in page.phase_label.text()
    assert page.recovery_card.isHidden()


def test_recovery_state_explains_checkpoint_and_source_safety(qtbot):
    page = TaskPage()
    qtbot.addWidget(page)

    page.set_workspace_state(TaskState.WAITING_FOR_NETWORK)

    assert page.recovery_card.isHidden() is False
    assert "检查点" in page.recovery_label.text()
    assert "不会删除源文件" in page.recovery_label.text()
    assert page.pause_button.isEnabled() is True


def test_start_request_during_worker_exit_runs_new_pending_work(qtbot):
    from nasmove.ui.queue_execution_controller import QueueExecutionController
    class Queue:
        calls = 0
        def run_next(self):
            self.calls += 1
        def has_pending_tasks(self):
            return self.calls == 1
    page = TaskPage()
    qtbot.addWidget(page)
    queue = Queue()
    controller = QueueExecutionController(queue, TaskController(page))
    controller.start()
    controller.start()
    qtbot.waitUntil(lambda: not controller.running)
    assert queue.calls == 2
