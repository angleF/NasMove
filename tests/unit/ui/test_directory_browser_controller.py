from pathlib import Path
from threading import Event

from nasmove.core.ports import RemoteEntry
from nasmove.ui.directory_browser_controller import DirectoryBrowserController
from nasmove.ui.directory_models import DirectorySide


def test_controller_reads_local_directory_off_the_ui_path(qtbot, tmp_path: Path) -> None:
    (tmp_path / "Folder").mkdir()
    (tmp_path / "movie.mov").write_bytes(b"abc")
    controller = DirectoryBrowserController()
    received = []
    controller.snapshot_ready.connect(received.append)

    controller.browse_local(tmp_path)

    qtbot.waitUntil(lambda: len(received) == 1)
    assert received[0].side is DirectorySide.LOCAL
    assert {entry.name for entry in received[0].entries} == {"Folder", "movie.mov"}


def test_controller_discards_remote_result_from_previous_profile(qtbot) -> None:
    release_first = Event()

    class Gateway:
        calls = 0

        def list_share_root(self):
            self.calls += 1
            if self.calls == 1:
                release_first.wait(2)
                return [RemoteEntry("old", True, 0)]
            return [RemoteEntry("new", True, 0)]

    controller = DirectoryBrowserController(gateway=Gateway())
    received = []
    controller.snapshot_ready.connect(received.append)

    controller.browse_remote(None, "profile-a")
    controller.browse_remote(None, "profile-b")
    release_first.set()
    qtbot.waitUntil(lambda: any(item.profile_id == "profile-b" for item in received))

    assert [item.profile_id for item in received] == ["profile-b"]
    controller.shutdown()


def test_invalidating_remote_browser_discards_in_flight_result(qtbot) -> None:
    release = Event()

    class Gateway:
        def list_share_root(self):
            release.wait(2)
            return [RemoteEntry("private", True, 0)]

    controller = DirectoryBrowserController(gateway=Gateway())
    received = []
    controller.snapshot_ready.connect(received.append)

    controller.browse_remote(None, "profile-a")
    controller.invalidate(DirectorySide.REMOTE)
    release.set()
    qtbot.waitUntil(lambda: not controller._threads)

    assert received == []
    controller.shutdown()


def test_controller_shutdown_waits_for_started_directory_read(qtbot) -> None:
    started = Event()
    release = Event()

    class Gateway:
        def list_share_root(self):
            started.set()
            release.wait(2)
            return []

    controller = DirectoryBrowserController(gateway=Gateway())
    controller.browse_remote(None, "profile-a")
    assert started.wait(1)
    thread = next(iter(controller._threads))

    release.set()

    assert controller.shutdown(timeout_ms=1_000) is True
    assert thread.isRunning() is False
    qtbot.waitUntil(lambda: not controller._threads)
