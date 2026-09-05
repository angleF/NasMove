def test_queue_never_runs_two_tasks_at_once(queue_fixture) -> None:
    queue_fixture.enqueue("task-a")
    queue_fixture.enqueue("task-b")
    queue_fixture.run_until_empty()

    assert queue_fixture.max_concurrent_tasks == 1
    assert queue_fixture.completed_order == ["task-a", "task-b"]
