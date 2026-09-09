from __future__ import annotations

from threading import get_ident

from nasmove.core.states import TaskState
from nasmove.transfer.transfer_engine import TaskResult
from nasmove.ui.queue_execution_controller import QueueExecutionController
from nasmove.ui.task_controller import TaskController
from nasmove.ui.task_page import TaskPage


class Queue:
    def __init__(self, results: list[TaskResult]) -> None:
        self.results = results
        self.thread_ids: list[int] = []

    def run_next(self) -> TaskResult | None:
        self.thread_ids.append(get_ident())
        return self.results.pop(0) if self.results else None


def test_queue_runs_off_ui_thread_until_empty_and_shows_result(qtbot) -> None:
    page = TaskPage()
    task_controller = TaskController(page)
    queue = Queue([TaskResult(True, TaskState.COMPLETED)])
    execution = QueueExecutionController(queue, task_controller)
    qtbot.addWidget(page)
    ui_thread_id = get_ident()

    execution.start()

    qtbot.waitUntil(lambda: execution.running is False)
    assert queue.thread_ids
    assert all(thread_id != ui_thread_id for thread_id in queue.thread_ids)
    assert page.result_summary.text() == "迁移完成"


def test_queue_execution_failure_is_reported_without_raw_error(qtbot) -> None:
    page = TaskPage()
    task_controller = TaskController(page)

    class FailingQueue:
        def run_next(self) -> None:
            raise RuntimeError("password=secret /private/source")

    execution = QueueExecutionController(FailingQueue(), task_controller)
    qtbot.addWidget(page)

    execution.start()

    qtbot.waitUntil(lambda: execution.running is False)
    assert page.status_label.text() == "执行已停止 · 需要处理"
    assert "unexpected_error" in page.error_details.toPlainText()
    assert "secret" not in page.error_details.toPlainText()


def test_start_while_running_does_not_create_second_worker(qtbot) -> None:
    page = TaskPage()
    task_controller = TaskController(page)

    class BlockingQueue:
        def __init__(self) -> None:
            self.calls = 0

        def run_next(self) -> None:
            self.calls += 1

    queue = BlockingQueue()
    execution = QueueExecutionController(queue, task_controller)
    qtbot.addWidget(page)

    execution.start()
    execution.start()

    qtbot.waitUntil(lambda: execution.running is False)
    assert queue.calls == 1
