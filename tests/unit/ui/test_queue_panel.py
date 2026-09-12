from dataclasses import replace

from nasmove.core.states import TaskState
from nasmove.transfer.progress import ProgressSnapshot
from nasmove.transfer.transfer_engine import TaskResult, TransferEvent
from nasmove.ui.queue_panel import QueuePanel
from tests.fixtures.builders import build_task_record, snapshot


def test_queue_panel_renders_one_row_per_task(qtbot) -> None:
    panel = QueuePanel()
    qtbot.addWidget(panel)
    task = replace(build_task_record(), total_files=12)

    panel.set_tasks((task,))

    assert panel.row_count() == 1
    assert "12 个文件" in panel.row_text(task.id)


def test_queue_panel_keeps_terminal_result_after_late_progress(qtbot) -> None:
    panel = QueuePanel()
    qtbot.addWidget(panel)
    task = replace(build_task_record(), state=TaskState.QUEUED)
    panel.set_tasks((task,))

    panel.apply_event(TransferEvent(task.id, TaskState.RUNNING))
    panel.apply_result(TaskResult(True, TaskState.COMPLETED, task_id=task.id))
    panel.apply_progress(task.id, snapshot(copy_percent=20))

    assert panel.state_text(task.id) == "已完成"
    assert panel.progress_value(task.id) == 100


def test_completed_with_warnings_reaches_full_progress_after_a_short_final_snapshot(qtbot) -> None:
    # The 0.25s notification throttle can drop the final 100% snapshot, so the
    # last delivered snapshot is one block short.  A successful terminal state
    # must still render 100%, exactly like ``_terminal_states()`` already claims.
    panel = QueuePanel()
    qtbot.addWidget(panel)
    task = replace(build_task_record(), state=TaskState.QUEUED)
    panel.set_tasks((task,))
    panel.select_task(task.id)

    panel.apply_event(TransferEvent(task.id, TaskState.RUNNING))
    panel.apply_progress(task.id, snapshot(copy_percent=99, verify_percent=99))
    assert panel.progress_value(task.id) == 99

    panel.apply_result(
        TaskResult(True, TaskState.COMPLETED_WITH_WARNINGS, warnings=("源文件仍保留",), task_id=task.id)
    )

    assert panel.progress_value(task.id) == 100
    assert panel.progress.value() == 100
    assert panel.progress.format() == "复制 100% · 校验 100%"


def test_failed_result_is_not_forced_to_full_progress(qtbot) -> None:
    panel = QueuePanel()
    qtbot.addWidget(panel)
    task = replace(build_task_record(), state=TaskState.QUEUED)
    panel.set_tasks((task,))
    panel.select_task(task.id)

    panel.apply_event(TransferEvent(task.id, TaskState.RUNNING))
    panel.apply_progress(task.id, snapshot(copy_percent=99, verify_percent=99))
    panel.apply_result(TaskResult(False, TaskState.FAILED, task_id=task.id))

    assert panel.progress_value(task.id) == 99
    assert panel.progress.value() == 99


def test_in_progress_percentages_round_instead_of_truncating(qtbot) -> None:
    panel = QueuePanel()
    qtbot.addWidget(panel)
    task = replace(build_task_record(), state=TaskState.QUEUED)
    panel.set_tasks((task,))
    panel.select_task(task.id)
    panel.apply_event(TransferEvent(task.id, TaskState.RUNNING))

    panel.apply_progress(
        task.id,
        ProgressSnapshot(
            total_bytes=400,
            completed_bytes=398,
            copied_bytes=199,
            verified_bytes=100,
            speed_bytes_per_second=1.0,
            eta_seconds=None,
        ),
    )

    assert panel.progress.format() == "复制 100% · 校验 50%"
    assert panel.progress.value() == 100


def test_in_progress_half_still_displays_fifty_percent(qtbot) -> None:
    panel = QueuePanel()
    qtbot.addWidget(panel)
    task = replace(build_task_record(), state=TaskState.QUEUED)
    panel.set_tasks((task,))
    panel.select_task(task.id)
    panel.apply_event(TransferEvent(task.id, TaskState.RUNNING))

    panel.apply_progress(task.id, snapshot(copy_percent=50, verify_percent=0))

    assert panel.progress.format() == "复制 50% · 校验 0%"
    assert panel.progress.value() == 25  # the bar averages copy + verification work


def test_queue_panel_emits_task_id_for_pause(qtbot) -> None:
    panel = QueuePanel()
    qtbot.addWidget(panel)
    task = replace(build_task_record(), state=TaskState.RUNNING)
    panel.set_tasks((task,))
    panel.select_task(task.id)

    with qtbot.waitSignal(panel.pause_requested) as signal:
        panel.pause_button.click()

    assert signal.args == [task.id]


def test_compact_queue_keeps_a_readable_task_count(qtbot) -> None:
    panel = QueuePanel()
    qtbot.addWidget(panel)
    panel.set_tasks((build_task_record(),))

    panel.set_compact(True)

    assert panel.count_label.isHidden()
    assert panel.fold_button.text() == "队\n1"
    margins = panel.layout().contentsMargins()
    assert margins.left() == margins.right() == 4
    assert panel.fold_button.width() == 40


from nasmove.core.model import TaskId


def test_queue_panel_switching_tasks_updates_progress_immediately(qtbot) -> None:
    panel = QueuePanel()
    qtbot.addWidget(panel)

    task_a = replace(build_task_record(), id=TaskId("task-a"), state=TaskState.QUEUED)
    task_b = replace(build_task_record(), id=TaskId("task-b"), state=TaskState.QUEUED)
    task_c = replace(build_task_record(), id=TaskId("task-c"), state=TaskState.QUEUED)

    panel.set_tasks((task_a, task_b, task_c))

    # Task A is running at 30% copy, 0% verify (average 15%)
    panel.select_task(task_a.id)
    panel.apply_event(TransferEvent(task_a.id, TaskState.RUNNING))
    panel.apply_progress(task_a.id, snapshot(copy_percent=30, verify_percent=0))
    assert panel.progress.value() == 15
    assert panel.progress.format() == "复制 30% · 校验 0%"

    # Task B is completed (100%)
    panel.apply_event(TransferEvent(task_b.id, TaskState.RUNNING))
    panel.apply_result(TaskResult(True, TaskState.COMPLETED, task_id=task_b.id))

    # Switch selection from A to B: should immediately show 100%, without leaking A's 15%
    panel.select_task(task_b.id)
    assert panel.progress.value() == 100
    assert panel.progress.format() == "复制 100% · 校验 100%"

    # Switch selection from B to C (queued, unstarted): should show "等待开始" and 0%
    panel.select_task(task_c.id)
    assert panel.progress.value() == 0
    assert panel.progress.format() == "等待开始"

    # Switch back to A: should immediately restore A's progress
    panel.select_task(task_a.id)
    assert panel.progress.value() == 15
    assert panel.progress.format() == "复制 30% · 校验 0%"

    # Clearing queue resets to no active task
    panel.set_tasks(())
    assert panel.progress.value() == 0
    assert panel.progress.format() == "尚无活动任务"
