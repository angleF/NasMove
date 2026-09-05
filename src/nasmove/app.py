from __future__ import annotations

import fcntl
import os
import sqlite3
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from typing import Any, cast

from nasmove.core.states import TaskState
from nasmove.persistence.sqlite_repository import SqliteTaskRepository
from nasmove.transfer.checkpoint_writer import CancellationToken
from nasmove.transfer.recovery import RecoveryCoordinator

_APPLICATION_DIR = Path("Library") / "Application Support" / "NasMove"
_DEFAULT_LOCK_NAME = "app.lock"
_DEFAULT_DATABASE_NAME = "nasmove.db"
_SHUTDOWN_TIMEOUT_SECONDS = 30.0
_ACTIVE_STATES = frozenset(
    {
        TaskState.QUEUED,
        TaskState.RUNNING,
        TaskState.INTERRUPTED,
        TaskState.WAITING_FOR_NETWORK,
        TaskState.VERIFYING,
        TaskState.COMMITTING,
        TaskState.DELETING_SOURCE,
    }
)


class DatabaseIntegrityError(RuntimeError):
    """The application database failed SQLite integrity verification."""


@dataclass(frozen=True, slots=True)
class StartupReport:
    started: bool
    already_running: bool = False
    interrupted_tasks: int = 0
    recovered_tasks: int = 0
    queued_tasks: int = 0
    paused_tasks: int = 0
    integrity_ok: bool = True
    error: str | None = None

    @property
    def credential_paused_tasks(self) -> int:
        return self.paused_tasks


@dataclass(frozen=True, slots=True)
class ShutdownResult:
    completed: bool
    timed_out: bool = False
    paused_tasks: int = 0
    message: str = ""
    error: str | None = None

    @property
    def safe_pausing(self) -> bool:
        return self.timed_out


class SingleInstanceLock:
    """An ownership-checked, non-blocking advisory lock for one app process."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        if not self.path.is_absolute():
            raise ValueError("application lock path must be absolute")
        self._fd: int | None = None

    def acquire(self) -> bool:
        self._prepare_parent()
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except FileExistsError:
            return False
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise PermissionError("application lock must be an owned regular file")
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            self._fd = fd
            return True
        except BaseException:
            os.close(fd)
            raise
        finally:
            if self._fd is None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _prepare_parent(self) -> None:
        parent = self.path.parent
        self._reject_symlink_ancestors(parent)
        try:
            info = os.lstat(parent)
        except FileNotFoundError:
            parent.mkdir(parents=True, mode=0o700)
            info = os.lstat(parent)
            os.chmod(parent, 0o700)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("application lock parent must be a real directory")
        if info.st_uid != os.getuid():
            raise PermissionError("application lock parent must be owned by the current user")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError("application lock parent must not be group or world accessible")

    @staticmethod
    def _reject_symlink_ancestors(path: Path) -> None:
        current = Path(path.anchor)
        for component in path.parts[1:]:
            current /= component
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                break
            if stat.S_ISLNK(info.st_mode):
                resolved = current.resolve()
                if current in {Path("/var"), Path("/tmp")} and resolved == Path("/private") / current.relative_to("/"):
                    continue
                raise ValueError("application lock path must not traverse a symlink")


class ApplicationService:
    """Compose durable startup recovery and cooperative application shutdown."""

    def __init__(
        self,
        repository: object | None = None,
        recovery: RecoveryCoordinator | object | None = None,
        queue: object | None = None,
        *,
        queue_coordinator: object | None = None,
        smb_gateway: object | None = None,
        credential_store: object | None = None,
        lock_path: Path | str | None = None,
        database_path: Path | str | None = None,
    ) -> None:
        self._owns_repository = repository is None
        if repository is None:
            database = Path(database_path) if database_path is not None else Path.home() / _APPLICATION_DIR / _DEFAULT_DATABASE_NAME
            repository = SqliteTaskRepository(database)
        self._repository = repository
        self._recovery = recovery
        self._queue = queue if queue is not None else queue_coordinator
        if self._queue is None:
            raise TypeError("queue coordinator is required")
        self._smb = smb_gateway
        self._credentials = credential_store
        default_lock = Path.home() / _APPLICATION_DIR / _DEFAULT_LOCK_NAME
        self._lock = SingleInstanceLock(lock_path or default_lock)
        self._started = False
        self._accepting = False
        self._shutdown_token = CancellationToken()

    @property
    def accepting_tasks(self) -> bool:
        return self._accepting

    def start(self) -> StartupReport:
        if self._started:
            return StartupReport(True)
        if not self._lock.acquire():
            return StartupReport(False, already_running=True, error="已有 NasMove 实例正在运行")
        try:
            self._integrity_check()
            interrupted = self._mark_interrupted()
            tasks = self._list_incomplete()
            queued = 0
            paused = 0
            for task in tasks:
                if self._has_credential(task):
                    self._enqueue(task.id)
                    queued += 1
                elif self._pause_task(task):
                    paused += 1
            self._started = True
            self._accepting = True
            return StartupReport(
                True,
                interrupted_tasks=interrupted,
                recovered_tasks=len(tasks),
                queued_tasks=queued,
                paused_tasks=paused,
            )
        except BaseException:
            self._lock.release()
            if self._owns_repository:
                self._close_repository()
            raise

    def enqueue(self, task_id: object) -> None:
        if not self._accepting:
            raise RuntimeError("application is stopping")
        self._enqueue(task_id)

    def request_shutdown(self, timeout: float = _SHUTDOWN_TIMEOUT_SECONDS) -> ShutdownResult:
        if not self._started:
            return ShutdownResult(True, message="应用未启动")
        self._accepting = False
        self._call_optional(self._queue, "stop_accepting")
        self._call_optional(self._queue, "request_pause")
        self._shutdown_token.request_pause()
        if not self._wait_for_boundary(max(0.0, timeout)):
            return ShutdownResult(
                False,
                timed_out=True,
                message="仍在安全暂停中",
            )
        self._call_optional(self._queue, "flush_and_checkpoint")
        paused = self._pause_active_tasks()
        try:
            self._close_smb()
        finally:
            try:
                self._close_repository()
            finally:
                self._lock.release()
        self._started = False
        return ShutdownResult(
            True,
            paused_tasks=paused,
            message="已安全暂停并关闭",
        )

    def _integrity_check(self) -> None:
        checker = getattr(self._repository, "integrity_check", None)
        if callable(checker):
            result = checker()
        else:
            connection = getattr(self._repository, "_connection", None)
            if not isinstance(connection, sqlite3.Connection):
                return
            row = connection.execute("PRAGMA integrity_check").fetchone()
            result = None if row is None else row[0]
        if result not in (None, True, "ok", 1):
            raise DatabaseIntegrityError("SQLite integrity check failed")

    def _mark_interrupted(self) -> int:
        marker = getattr(self._recovery, "mark_active_tasks_interrupted", None)
        if callable(marker):
            return int(marker())
        marker = getattr(self._repository, "mark_active_tasks_interrupted", None)
        if not callable(marker):
            raise TypeError("repository must provide mark_active_tasks_interrupted()")
        return int(marker())

    def _list_incomplete(self) -> list[Any]:
        listing = getattr(self._recovery, "list_incomplete_tasks", None)
        if callable(listing):
            return list(listing())
        listing = getattr(self._repository, "list_incomplete_tasks", None)
        if not callable(listing):
            raise TypeError("repository must provide list_incomplete_tasks()")
        return list(listing())

    def _has_credential(self, task: Any) -> bool:
        if self._credentials is None:
            return True
        try:
            getter = cast(Any, self._credentials).get_password
            password = getter(task.connection.profile_id)
        except Exception:  # noqa: BLE001 - credential failures fail closed
            return False
        return type(password) is str and bool(password)

    def _pause_task(self, task: Any) -> bool:
        if task.state is TaskState.PAUSED:
            return True
        if task.state not in _ACTIVE_STATES:
            return False
        transition = getattr(self._repository, "transition_task", None)
        if not callable(transition):
            return False
        transition(task.id, task.state, TaskState.PAUSED)
        return True

    def _enqueue(self, task_id: object) -> None:
        enqueue = getattr(self._queue, "enqueue", None)
        if not callable(enqueue):
            raise TypeError("queue coordinator must provide enqueue()")
        enqueue(task_id)

    def _wait_for_boundary(self, timeout: float) -> bool:
        waiter = getattr(self._queue, "wait_for_safe_boundary", None)
        if callable(waiter):
            return self._bounded_wait(waiter, timeout)
        waiter = getattr(self._queue, "wait_for_boundary", None)
        if callable(waiter):
            return self._bounded_wait(waiter, timeout)
        return True

    @staticmethod
    def _bounded_wait(waiter: Callable[[float], object], timeout: float) -> bool:
        result: list[object] = []
        completed = False

        def wait() -> None:
            nonlocal completed
            try:
                result.append(waiter(timeout))
            finally:
                completed = True

        worker = Thread(target=wait, name="nasmove-shutdown-wait", daemon=True)
        worker.start()
        worker.join(timeout)
        if not completed:
            return False
        return bool(result[0]) if result else False

    def _pause_active_tasks(self) -> int:
        tasks = self._list_incomplete()
        paused = 0
        for task in tasks:
            if self._pause_task(task):
                paused += 1
        return paused

    def _close_smb(self) -> None:
        if self._smb is not None:
            self._call_optional(self._smb, "disconnect")

    def _close_repository(self) -> None:
        self._call_optional(self._repository, "close")

    @staticmethod
    def _call_optional(target: object, method_name: str, *args: object) -> object | None:
        method = getattr(target, method_name, None)
        if not callable(method):
            return None
        return cast(object, method(*args))


__all__ = [
    "ApplicationService",
    "DatabaseIntegrityError",
    "ShutdownResult",
    "SingleInstanceLock",
    "StartupReport",
]
