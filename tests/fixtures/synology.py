from __future__ import annotations

import os
import re
import secrets
import shlex
import socket
import subprocess
import tempfile
import threading
import time
import tracemalloc
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from nasmove.core.errors import DomainValidationError
from nasmove.core.model import ConnectionConfig, ConnectionProfileId, RemotePath, TaskRecord
from nasmove.core.ports import SessionInfo
from nasmove.core.retry import RetryPolicy
from nasmove.core.states import (
    ConflictPolicy,
    TaskState,
    TransferAction,
    VerificationPolicy,
)
from nasmove.localio.files import PosixLocalFileGateway
from nasmove.localio.hashing import sha256_stream
from nasmove.persistence.sqlite_repository import SqliteTaskRepository
from nasmove.planning.task_planner import PlanRequest, TaskPlanner
from nasmove.smb.error_mapping import map_smb_error, redacted_error_code
from nasmove.smb.smbprotocol_gateway import SmbProtocolGateway
from nasmove.transfer.checkpoint_writer import CancellationToken, CheckpointWriter
from nasmove.transfer.commit import TargetCommitter
from nasmove.transfer.deletion import SourceDeletionService
from nasmove.transfer.item_worker import TransferItemWorker
from nasmove.transfer.recovery import RecoveryCoordinator
from nasmove.transfer.retrying_runner import RetryingTaskRunner
from nasmove.transfer.transfer_engine import TransferEngine
from nasmove.transfer.verification import IntegrityVerifier
from tests.fixtures.fault_proxy import FaultProxy, LocalTcpFaultProxy


@dataclass(frozen=True, slots=True)
class SynologyResult:
    source_sha256: str
    target_sha256: str
    source_exists: bool
    max_replayed_bytes: int
    disconnects: int = 0


@dataclass(frozen=True, slots=True)
class SpacePreflightResult:
    source_exists: bool
    remote_root_unchanged: bool
    error_code: str


@dataclass(frozen=True, slots=True)
class FaultScenarioResult:
    error_category: str
    error_code: str
    error_type: str
    source_exists: bool
    final_target_exists: bool


@dataclass(frozen=True, slots=True)
class TargetRaceResult:
    success: bool
    existing_target_preserved: bool
    final_path_was_renamed: bool
    source_exists: bool


@dataclass(frozen=True, slots=True)
class ManyFilesResult:
    completed_files: int
    verified_samples: int
    peak_memory_bytes: int
    source_files_retained: int


@dataclass(frozen=True, slots=True)
class DirectoryTreeResult:
    nested_file_verified: bool
    empty_directory_created: bool
    source_tree_retained: bool


@dataclass(frozen=True, slots=True)
class ConcurrentFilesResult:
    completed_files: int
    verified_files: int
    distinct_worker_gateways: int
    distinct_worker_sessions: int
    peak_parallel_workers: int
    source_files_retained: int


class _FixedCredentialStore:
    def __init__(self, password: str) -> None:
        self._password = password

    def get_password(self, _profile_id: ConnectionProfileId) -> str:
        return self._password


class _SynologyAdmin:
    _EXPECT_SCRIPT = r"""
set timeout 45
log_user 0
set sudo_authenticated 0
spawn ssh -tt -o ConnectTimeout=8 -o StrictHostKeyChecking=yes $env(NASMOVE_USER)@$env(NASMOVE_SYNOLOGY_HOST) $env(NASMOVE_ADMIN_COMMAND)
expect {
    -exact "SUDO_PROMPT" { set sudo_authenticated 1; send -- "$env(NASMOVE_PASSWORD)\r"; exp_continue }
    -exact "NASMOVE_SECRET_PROMPT" { send -- "$env(NASMOVE_ADMIN_SECRET)\r"; exp_continue }
    -re {(?i)password:} { send -- "$env(NASMOVE_PASSWORD)\r"; exp_continue }
    eof {
        catch wait result
        set code [lindex $result 3]
        if {$sudo_authenticated && $code == 255} { exit 85 }
        exit $code
    }
    timeout { exit 3 }
}
"""

    def __init__(self, share: str, test_root: str) -> None:
        self._share = share
        self._relative_root = f"{share.strip('/')}/{test_root.strip('/')}"

    def child_command(self, child: str, action: str) -> None:
        if not child or "/" in child or child in {".", ".."}:
            raise ValueError("admin child must be one safe path component")
        relative = shlex.quote(f"{self._relative_root}/{child}")
        script = (
            f"set -- /volume*/{relative}; "
            '[ "$#" -eq 1 ] && [ -d "$1" ] || exit 66; '
            f"{action}"
        )
        self._run(script)

    def create_test_user(self, username: str, password: str) -> None:
        self._validate_temporary_username(username)
        quoted_user = shlex.quote(username)
        quoted_share = shlex.quote(self._share)
        script = (
            "printf NASMOVE_SECRET_PROMPT >&2; "
            "IFS= read -r nasmove_secret; "
            f"/usr/syno/sbin/synouser --add {quoted_user} \"$nasmove_secret\" "
            "'NasMove fault test' 0 '' 0; "
            "status=$?; unset nasmove_secret; [ \"$status\" -eq 0 ] || exit \"$status\"; "
            f"/usr/syno/sbin/synoshare --setuser {quoted_share} RW + {quoted_user}"
        )
        self._run(script, secret=password)

    def revoke_test_user(self, username: str) -> None:
        self._validate_temporary_username(username)
        quoted_user = shlex.quote(username)
        quoted_share = shlex.quote(self._share)
        self._run(
            f"/usr/syno/sbin/synoshare --setuser {quoted_share} RW - {quoted_user}; "
            f"/usr/syno/sbin/synoshare --setuser {quoted_share} NA + {quoted_user}"
        )

    def delete_test_user(self, username: str) -> None:
        self._validate_temporary_username(username)
        quoted_user = shlex.quote(username)
        quoted_share = shlex.quote(self._share)
        self._run(
            f"/usr/syno/sbin/synoshare --setuser {quoted_share} NA - {quoted_user}; "
            f"/usr/syno/sbin/synoshare --setuser {quoted_share} RW - {quoted_user}; "
            f"/usr/syno/sbin/synouser --del {quoted_user}"
        )

    def reboot(self) -> None:
        self._run("/usr/sbin/reboot", accepted_returncodes=(0, 85))

    @staticmethod
    def _validate_temporary_username(username: str) -> None:
        if re.fullmatch(r"nasmove_test_[a-z0-9]{4,20}", username) is None:
            raise ValueError("temporary username is unsafe")

    def _run(
        self,
        script: str,
        *,
        secret: str = "",
        accepted_returncodes: tuple[int, ...] = (0,),
    ) -> None:
        command = f"sudo -S -p SUDO_PROMPT sh -c {shlex.quote(script)}"
        environment = os.environ.copy()
        environment["NASMOVE_ADMIN_COMMAND"] = command
        environment["NASMOVE_ADMIN_SECRET"] = secret
        try:
            completed = subprocess.run(
                ["/usr/bin/expect", "-c", self._EXPECT_SCRIPT],
                env=environment,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("Synology administrative command failed") from error
        if completed.returncode not in accepted_returncodes:
            raise RuntimeError(
                f"Synology administrative command failed with code {completed.returncode}"
            )


class _TargetRaceGateway:
    def __init__(self, gateway: SmbProtocolGateway, occupied_payload: bytes) -> None:
        self._gateway = gateway
        self._occupied_payload = occupied_payload
        self.injected_path: RemotePath | None = None

    def rename_exclusive(self, source: RemotePath, target: RemotePath) -> None:
        if self.injected_path is None:
            with self._gateway.create_exclusive(target) as stream:
                stream.write(self._occupied_payload)
                stream.flush()
            self.injected_path = target
        self._gateway.rename_exclusive(source, target)

    def __getattr__(self, name: str) -> object:
        return getattr(self._gateway, name)


def _ntlm_gateway() -> SmbProtocolGateway:
    """Match the NTLM gateways these fault scenarios connect through the proxy."""
    return SmbProtocolGateway(auth_protocol="ntlm")


class _WorkerEventSink:
    """Record item-worker events so the fixture mirrors production wiring.

    ``desktop_app.build_engine`` hands every item worker the shared UI event
    sink; the isolated NAS gate has no UI, but the workers must still be built
    the same way, so their events are recorded here instead of dropped.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[object] = []

    def publish(self, event: object) -> None:
        with self._lock:
            self.events.append(event)


class SynologyFixture:
    """Production-component harness restricted to an isolated Synology share."""

    def __init__(self) -> None:
        self.host = os.environ["NASMOVE_SYNOLOGY_HOST"]
        self.port = int(os.environ.get("NASMOVE_SYNOLOGY_PORT", "445"))
        self.share = os.environ["NASMOVE_SYNOLOGY_SHARE"]
        self.test_root = os.environ["NASMOVE_SYNOLOGY_TEST_ROOT"].strip("/")
        self.username = os.environ.get("NASMOVE_TEST_SMB_USERNAME") or os.environ[
            "NASMOVE_USER"
        ]
        self.password = os.environ.get("NASMOVE_TEST_SMB_PASSWORD") or os.environ[
            "NASMOVE_PASSWORD"
        ]
        if not self.test_root.startswith("NasMoveTest/"):
            raise ValueError("NASMOVE_SYNOLOGY_TEST_ROOT must be under NasMoveTest/")
        self._worker_records_lock = threading.Lock()
        self._record_workers = False
        self._worker_gateways: list[object] = []
        self._worker_sessions: list[object] = []
        self._worker_thread_ids: list[int] = []

    def transfer_with_disconnects(
        self,
        *,
        disconnect_percentages: list[int],
        size_gib: int | None = None,
        size_mib: int | None = None,
    ) -> SynologyResult:
        size = self._transfer_size(size_gib=size_gib, size_mib=size_mib)
        percentages = self._validate_percentages(disconnect_percentages)
        with tempfile.TemporaryDirectory(prefix="nasmove-synology-") as temporary:
            source = Path(temporary).resolve() / f"disconnect-{uuid.uuid4().hex}.bin"
            with source.open("wb") as stream:
                stream.write(b"NasMove Synology fault probe\n")
                stream.truncate(size)
            database_path = Path(temporary).resolve() / "state.sqlite3"
            with LocalTcpFaultProxy(self.host, self.port) as proxy:
                return self._run_disconnect_scenario(
                    source,
                    database_path,
                    proxy,
                    percentages,
                )

    def reject_plan_beyond_free_space(self) -> SpacePreflightResult:
        gateway = SmbProtocolGateway()
        root = RemotePath(self.test_root)
        try:
            config = ConnectionConfig(
                profile_id=ConnectionProfileId("synology-space-probe"),
                display_name="Synology space probe",
                host=self.host,
                port=self.port,
                share=self.share,
                username=self.username,
                require_encryption=True,
            )
            gateway.connect(config, self.password)
            before = tuple(gateway.list_dir(root))
            available = gateway.free_space(root)
            with tempfile.TemporaryDirectory(prefix="nasmove-space-") as temporary:
                source = Path(temporary).resolve() / "oversized-sparse.bin"
                with source.open("wb") as stream:
                    stream.truncate(available + (1 << 30))
                try:
                    TaskPlanner(PosixLocalFileGateway(), gateway).plan(
                        PlanRequest(
                            name="Synology space preflight probe",
                            connection=config,
                            sources=(source,),
                            target_root=root,
                            action=TransferAction.COPY,
                            conflict_policy=ConflictPolicy.AUTO_RENAME,
                            verification_policy=VerificationPolicy.FULL,
                        )
                    )
                except DomainValidationError as error:
                    error_code = (
                        "insufficient_space"
                        if str(error) == "insufficient NAS free space for planned transfer"
                        else "unexpected_validation_error"
                    )
                else:
                    raise AssertionError("oversized transfer plan was unexpectedly accepted")
                source_exists = source.exists()
            after = tuple(gateway.list_dir(root))
            return SpacePreflightResult(source_exists, before == after, error_code)
        finally:
            gateway.disconnect()

    def transfer_into_revoked_directory(self) -> FaultScenarioResult:
        return self._run_admin_fault("permission", mount_tmpfs=False)

    def transfer_into_small_tmpfs(self) -> FaultScenarioResult:
        return self._run_admin_fault("disk-full", mount_tmpfs=True)

    def transfer_while_source_changes(self) -> FaultScenarioResult:
        with tempfile.TemporaryDirectory(prefix="nasmove-source-change-") as temporary:
            source = Path(temporary).resolve() / f"source-change-{uuid.uuid4().hex}.bin"
            with source.open("wb") as stream:
                stream.write(b"A")
                stream.truncate(128 * 1024 * 1024)
            repository = SqliteTaskRepository(Path(temporary).resolve() / "state.sqlite3")
            gateway = SmbProtocolGateway(auth_protocol="ntlm")
            final_path: RemotePath | None = None
            temp_path: RemotePath | None = None
            with LocalTcpFaultProxy(self.host, self.port) as proxy:
                config = ConnectionConfig(
                    profile_id=ConnectionProfileId("synology-source-change"),
                    display_name="Synology source change",
                    host="127.0.0.1",
                    port=proxy.data_port,
                    share=self.share,
                    username=self.username,
                    require_encryption=True,
                )
                try:
                    gateway.connect(config, self.password)
                    planned = self._plan_single(
                        repository,
                        gateway,
                        config,
                        source,
                        "Synology source change probe",
                    )
                    item = repository.list_items(planned.task.id)[0]
                    final_path, temp_path = item.final_path, item.temp_path
                    gateway.reset_connection()

                    mutation_performed = threading.Event()

                    def mutate_source() -> None:
                        if not proxy.wait_for_additional_upstream_bytes(
                            32 * 1024 * 1024, timeout=120
                        ):
                            return
                        before = source.stat()
                        with source.open("r+b", buffering=0) as stream:
                            stream.seek(0)
                            stream.write(b"B")
                            os.fsync(stream.fileno())
                        os.utime(
                            source,
                            ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
                        )
                        mutation_performed.set()

                    injector = threading.Thread(target=mutate_source, daemon=True)
                    injector.start()
                    runner = RetryingTaskRunner(
                        repository,
                        _FixedCredentialStore(self.password),
                        self._engine_factory(
                            repository, gateway, gateway_factory=_ntlm_gateway
                        ),
                        sleeper=lambda _delay: None,
                        jitter=lambda: 0.0,
                    )
                    result = runner.run_task(planned.task.id, CancellationToken())
                    injector.join(timeout=2)
                    if not mutation_performed.is_set():
                        raise AssertionError("source mutation was not injected")
                    error_code = (
                        "full_verification_failed"
                        if result.error is not None
                        and str(result.error) == "full verification failed"
                        else "none"
                        if result.error is None
                        else redacted_error_code(result.error)
                    )
                    return FaultScenarioResult(
                        "unknown"
                        if result.error is None
                        else map_smb_error(result.error).category.value,
                        error_code,
                        "None" if result.error is None else type(result.error).__name__,
                        source.exists(),
                        gateway.stat(item.final_path) is not None,
                    )
                finally:
                    self._cleanup_remote(gateway, final_path, temp_path)
                    gateway.disconnect()
                    repository.close()

    def transfer_with_target_race(self) -> TargetRaceResult:
        occupied_payload = b"pre-existing target must be preserved"
        with tempfile.TemporaryDirectory(prefix="nasmove-target-race-") as temporary:
            source = Path(temporary).resolve() / f"target-race-{uuid.uuid4().hex}.bin"
            source.write_bytes(b"new transfer payload")
            repository = SqliteTaskRepository(Path(temporary).resolve() / "state.sqlite3")
            gateway = SmbProtocolGateway()
            wrapped = _TargetRaceGateway(gateway, occupied_payload)
            final_paths: list[RemotePath] = []
            temp_path: RemotePath | None = None
            config = self._direct_config("synology-target-race")
            try:
                gateway.connect(config, self.password)
                planned = self._plan_single(
                    repository,
                    gateway,
                    config,
                    source,
                    "Synology target race probe",
                )
                item = repository.list_items(planned.task.id)[0]
                planned_final = item.final_path
                final_paths.append(planned_final)
                temp_path = item.temp_path
                gateway.reset_connection()
                runner = RetryingTaskRunner(
                    repository,
                    _FixedCredentialStore(self.password),
                    self._engine_factory(
                        repository, wrapped, gateway_factory=lambda: wrapped
                    ),
                    sleeper=lambda _delay: None,
                    jitter=lambda: 0.0,
                )
                result = runner.run_task(planned.task.id, CancellationToken())
                completed = repository.get_item(item.id)
                final_paths.append(completed.final_path)
                with gateway.open_read(planned_final) as stream:
                    existing_preserved = stream.read() == occupied_payload
                return TargetRaceResult(
                    result.success,
                    existing_preserved,
                    completed.final_path != planned_final,
                    source.exists(),
                )
            finally:
                for path in (*final_paths, temp_path):
                    if path is None:
                        continue
                    with suppress(Exception):
                        if gateway.stat(path) is not None:
                            gateway.remove_file(path)
                gateway.disconnect()
                repository.close()

    def transfer_across_nas_restart(self, *, size_mib: int) -> SynologyResult:
        if os.environ.get("NASMOVE_TEST_DSM_RESTART") != "1":
            pytest.skip("set NASMOVE_TEST_DSM_RESTART=1 to authorize a real DSM restart")
        size = self._transfer_size(size_gib=None, size_mib=size_mib)
        with tempfile.TemporaryDirectory(prefix="nasmove-restart-") as temporary:
            source = Path(temporary).resolve() / f"restart-{uuid.uuid4().hex}.bin"
            with source.open("wb") as stream:
                stream.write(b"NasMove DSM restart probe\n")
                stream.truncate(size)
            repository = SqliteTaskRepository(Path(temporary).resolve() / "state.sqlite3")
            gateway = SmbProtocolGateway(auth_protocol="ntlm")
            final_path: RemotePath | None = None
            temp_path: RemotePath | None = None
            with LocalTcpFaultProxy(self.host, self.port) as proxy:
                config = ConnectionConfig(
                    profile_id=ConnectionProfileId("synology-restart-probe"),
                    display_name="Synology restart probe",
                    host="127.0.0.1",
                    port=proxy.data_port,
                    share=self.share,
                    username=self.username,
                    require_encryption=True,
                )
                restart_completed = threading.Event()
                restart_errors: list[BaseException] = []
                maximum_replay = [0]
                try:
                    gateway.connect(config, self.password)
                    planned = self._plan_single(
                        repository,
                        gateway,
                        config,
                        source,
                        "Synology restart probe",
                    )
                    item = repository.list_items(planned.task.id)[0]
                    final_path, temp_path = item.final_path, item.temp_path
                    gateway.reset_connection()

                    def restart_nas() -> None:
                        try:
                            if not proxy.wait_for_additional_upstream_bytes(
                                64 * 1024 * 1024, timeout=180
                            ):
                                raise RuntimeError("restart injection threshold was not reached")
                            _SynologyAdmin(self.share, self.test_root).reboot()
                            if not self._wait_for_port(unreachable=True, timeout=90):
                                raise RuntimeError("NAS SMB port did not become unreachable")
                            restart_completed.set()
                        except BaseException as error:  # noqa: BLE001
                            restart_errors.append(error)

                    def engine_factory(task: TaskRecord, password: str) -> TransferEngine:
                        gateway.connect(task.connection, password)
                        current = repository.get_item(item.id)
                        remote = gateway.stat(current.temp_path)
                        if remote is not None:
                            maximum_replay[0] = max(
                                maximum_replay[0],
                                max(0, remote.size - current.confirmed_offset),
                            )
                        return self._build_engine(
                            repository, password, gateway_factory=_ntlm_gateway
                        )

                    injector = threading.Thread(target=restart_nas, daemon=True)
                    injector.start()
                    runner = RetryingTaskRunner(
                        repository,
                        _FixedCredentialStore(self.password),
                        engine_factory,
                        retry_policy=RetryPolicy(
                            delays=(2.0, 4.0, 8.0, 10.0),
                            waiting_probe_seconds=10.0,
                        ),
                        sleeper=time.sleep,
                        jitter=lambda: 0.0,
                    )
                    result = runner.run_task(planned.task.id, CancellationToken())
                    injector.join(timeout=100)
                    if restart_errors:
                        raise RuntimeError("DSM restart injection failed") from restart_errors[0]
                    if not restart_completed.is_set():
                        raise AssertionError("DSM restart was not injected")
                    if not self._wait_for_port(unreachable=False, timeout=300):
                        raise AssertionError("NAS SMB port did not recover after restart")
                    if not result.success:
                        error_type = "None" if result.error is None else type(result.error).__name__
                        error_code = (
                            "none" if result.error is None else redacted_error_code(result.error)
                        )
                        raise AssertionError(
                            "transfer did not recover: "
                            f"{result.state.value} ({error_type}, {error_code})"
                        )
                    completed = repository.get_item(item.id)
                    final_path = completed.final_path
                    with source.open("rb") as local_stream:
                        source_hash = sha256_stream(local_stream).hexdigest
                    with gateway.open_read(completed.final_path) as remote_stream:
                        target_hash = sha256_stream(remote_stream).hexdigest
                    gateway.remove_file(completed.final_path)
                    final_path = None
                    return SynologyResult(
                        source_hash,
                        target_hash,
                        source.exists(),
                        maximum_replay[0],
                        disconnects=1,
                    )
                finally:
                    if restart_completed.is_set():
                        self._wait_for_port(unreachable=False, timeout=300)
                    gateway.reset_connection()
                    with suppress(Exception):
                        gateway.connect(
                            self._direct_config("synology-restart-cleanup"), self.password
                        )
                        self._cleanup_remote(gateway, final_path, temp_path)
                    gateway.disconnect()
                    repository.close()

    def transfer_many_small_files(
        self,
        *,
        file_count: int,
        sample_count: int,
    ) -> ManyFilesResult:
        if file_count <= 0 or sample_count <= 0 or sample_count > file_count:
            raise ValueError("small-file counts must be positive and samples cannot exceed files")
        child = f"many-files-{uuid.uuid4().hex}"
        target_root = RemotePath(f"{self.test_root}/{child}")
        admin = _SynologyAdmin(self.share, self.test_root)
        gateway = SmbProtocolGateway()
        with tempfile.TemporaryDirectory(prefix="nasmove-many-files-") as temporary:
            local_root = Path(temporary).resolve() / "sources"
            local_root.mkdir()
            sources: list[Path] = []
            for index in range(file_count):
                source = local_root / f"file-{index:06d}.bin"
                size = 1024 + (index * 7919) % (63 * 1024 + 1)
                pattern = index.to_bytes(4, "little")
                with source.open("wb") as stream:
                    stream.write((pattern * ((size + 3) // 4))[:size])
                sources.append(source)

            repository = SqliteTaskRepository(Path(temporary).resolve() / "state.sqlite3")
            config = self._direct_config("synology-many-files")
            tracemalloc.start()
            try:
                gateway.connect(config, self.password)
                gateway.make_dir(target_root)
                planned = TaskPlanner(
                    PosixLocalFileGateway(), gateway, repository
                ).plan(
                    PlanRequest(
                        name="Synology many small files",
                        connection=config,
                        sources=tuple(sources),
                        target_root=target_root,
                        action=TransferAction.COPY,
                        conflict_policy=ConflictPolicy.AUTO_RENAME,
                        verification_policy=VerificationPolicy.FULL,
                    )
                )
                repository.transition_task(
                    planned.task.id,
                    TaskState.PREFLIGHT,
                    TaskState.QUEUED,
                )
                gateway.reset_connection()
                runner = RetryingTaskRunner(
                    repository,
                    _FixedCredentialStore(self.password),
                    self._engine_factory(repository, gateway),
                    sleeper=lambda _delay: None,
                    jitter=lambda: 0.0,
                )
                result = runner.run_task(planned.task.id, CancellationToken())
                if not result.success:
                    raise AssertionError(f"many-file transfer failed: {result.state.value}")
                items = repository.list_items(planned.task.id)
                sample_indexes = {
                    index * (file_count - 1) // max(1, sample_count - 1)
                    for index in range(sample_count)
                }
                verified = 0
                for index in sorted(sample_indexes):
                    item = items[index]
                    with item.source_path.open("rb") as local_stream:
                        local_hash = sha256_stream(local_stream).hexdigest
                    with gateway.open_read(item.final_path) as remote_stream:
                        remote_hash = sha256_stream(remote_stream).hexdigest
                    if local_hash != remote_hash:
                        raise AssertionError(f"small-file sample {index} failed SHA-256")
                    verified += 1
                _, peak = tracemalloc.get_traced_memory()
                return ManyFilesResult(
                    completed_files=result.completed_items,
                    verified_samples=verified,
                    peak_memory_bytes=peak,
                    source_files_retained=sum(source.exists() for source in sources),
                )
            finally:
                tracemalloc.stop()
                gateway.disconnect()
                repository.close()
                with suppress(Exception):
                    admin.child_command(child, 'rm -rf -- "$1"')

    def transfer_directory_tree(self) -> DirectoryTreeResult:
        child = f"directory-tree-{uuid.uuid4().hex}"
        target_root = RemotePath(f"{self.test_root}/{child}")
        admin = _SynologyAdmin(self.share, self.test_root)
        gateway = SmbProtocolGateway()
        with tempfile.TemporaryDirectory(prefix="nasmove-directory-tree-") as temporary:
            source_root = Path(temporary).resolve() / "source"
            nested = source_root / "nested"
            empty = source_root / "empty"
            nested.mkdir(parents=True)
            empty.mkdir()
            payload = b"NasMove nested directory verification\n"
            source_file = nested / "payload.bin"
            source_file.write_bytes(payload)
            repository = SqliteTaskRepository(Path(temporary).resolve() / "state.sqlite3")
            config = self._direct_config("synology-directory-tree")
            try:
                gateway.connect(config, self.password)
                gateway.make_dir(target_root)
                planned = TaskPlanner(PosixLocalFileGateway(), gateway, repository).plan(
                    PlanRequest(
                        name="Synology directory tree",
                        connection=config,
                        sources=(source_root,),
                        target_root=target_root,
                        action=TransferAction.COPY,
                        conflict_policy=ConflictPolicy.AUTO_RENAME,
                        verification_policy=VerificationPolicy.FULL,
                    )
                )
                repository.transition_task(
                    planned.task.id, TaskState.PREFLIGHT, TaskState.QUEUED
                )
                result = self._build_engine(repository, self.password).run_task(
                    planned.task.id, CancellationToken()
                )
                if not result.success:
                    raise AssertionError(f"directory-tree transfer failed: {result.state.value}")
                remote_file = RemotePath(f"{target_root.value}/source/nested/payload.bin")
                remote_empty = RemotePath(f"{target_root.value}/source/empty")
                with gateway.open_read(remote_file) as stream:
                    nested_file_verified = stream.read() == payload
                empty_stat = gateway.stat(remote_empty)
                return DirectoryTreeResult(
                    nested_file_verified=nested_file_verified,
                    empty_directory_created=(
                        empty_stat is not None
                        and empty_stat.is_directory
                        and gateway.list_dir(remote_empty) == []
                    ),
                    source_tree_retained=source_file.exists() and empty.is_dir(),
                )
            finally:
                gateway.disconnect()
                repository.close()
                with suppress(Exception):
                    admin.child_command(child, 'rm -rf -- "$1"')

    def transfer_concurrent_files(
        self,
        *,
        file_count: int = 6,
        max_parallel_items: int = 2,
    ) -> ConcurrentFilesResult:
        """Transfer several files through the production concurrent path.

        The task snapshot fixes ``max_parallel_items`` at 2 (or more), so the
        engine runs its per-item worker factory concurrently.  Every item must
        land with a matching SHA-256 and must have been handled by its own
        gateway, session and worker thread.
        """
        if max_parallel_items < 2 or file_count < max_parallel_items:
            raise ValueError("concurrent scenario needs a concurrency above one and enough files")
        child = f"concurrent-files-{uuid.uuid4().hex}"
        target_root = RemotePath(f"{self.test_root}/{child}")
        admin = _SynologyAdmin(self.share, self.test_root)
        gateway = SmbProtocolGateway()
        with tempfile.TemporaryDirectory(prefix="nasmove-concurrent-files-") as temporary:
            local_root = Path(temporary).resolve() / "sources"
            local_root.mkdir()
            sources: list[Path] = []
            for index in range(file_count):
                source = local_root / f"file-{index:06d}.bin"
                size = 4096 + (index * 7919) % (256 * 1024 + 1)
                pattern = (index + 1).to_bytes(4, "little")
                with source.open("wb") as stream:
                    stream.write((pattern * ((size + 3) // 4))[:size])
                sources.append(source)

            repository = SqliteTaskRepository(Path(temporary).resolve() / "state.sqlite3")
            config = replace(
                self._direct_config("synology-concurrent-files"),
                max_parallel_items=max_parallel_items,
            )
            self._reset_worker_records()
            self._record_workers = True
            try:
                gateway.connect(config, self.password)
                gateway.make_dir(target_root)
                planned = TaskPlanner(PosixLocalFileGateway(), gateway, repository).plan(
                    PlanRequest(
                        name="Synology concurrent files",
                        connection=config,
                        sources=tuple(sources),
                        target_root=target_root,
                        action=TransferAction.COPY,
                        conflict_policy=ConflictPolicy.AUTO_RENAME,
                        verification_policy=VerificationPolicy.FULL,
                    )
                )
                repository.transition_task(
                    planned.task.id, TaskState.PREFLIGHT, TaskState.QUEUED
                )
                gateway.reset_connection()
                runner = RetryingTaskRunner(
                    repository,
                    _FixedCredentialStore(self.password),
                    self._engine_factory(repository, gateway),
                    sleeper=lambda _delay: None,
                    jitter=lambda: 0.0,
                )
                result = runner.run_task(planned.task.id, CancellationToken())
                if not result.success:
                    raise AssertionError(f"concurrent transfer failed: {result.state.value}")
                items = repository.list_items(planned.task.id)
                if len(items) != file_count:
                    raise AssertionError("concurrent scenario planned the wrong item count")
                verified = 0
                for item in items:
                    with item.source_path.open("rb") as local_stream:
                        local_hash = sha256_stream(local_stream).hexdigest
                    with gateway.open_read(item.final_path) as remote_stream:
                        remote_hash = sha256_stream(remote_stream).hexdigest
                    if local_hash != remote_hash:
                        raise AssertionError(
                            f"concurrent file {item.relative_path} failed SHA-256"
                        )
                    verified += 1
                gateways, sessions, threads = self._worker_resource_evidence()
                # The serial compatibility branch would record nothing here; the
                # concurrent path must have assembled one gateway and session
                # per file, on more than one pool thread.
                if gateways != file_count:
                    raise AssertionError(
                        "concurrent workers did not each own a distinct gateway"
                    )
                if sessions != file_count:
                    raise AssertionError(
                        "concurrent workers did not each own a distinct session"
                    )
                if threads < 2:
                    raise AssertionError(
                        "concurrent scenario never ran two workers in parallel"
                    )
                return ConcurrentFilesResult(
                    completed_files=result.completed_items,
                    verified_files=verified,
                    distinct_worker_gateways=gateways,
                    distinct_worker_sessions=sessions,
                    peak_parallel_workers=threads,
                    source_files_retained=sum(source.exists() for source in sources),
                )
            finally:
                self._record_workers = False
                gateway.disconnect()
                repository.close()
                with suppress(Exception):
                    admin.child_command(child, 'rm -rf -- "$1"')

    def _run_admin_fault(self, label: str, *, mount_tmpfs: bool) -> FaultScenarioResult:
        child = f"{label}-{uuid.uuid4().hex}"
        target_root = RemotePath(f"{self.test_root}/{child}")
        gateway = SmbProtocolGateway()
        repository: SqliteTaskRepository | None = None
        admin = _SynologyAdmin(self.share, self.test_root)
        mounted = False
        temporary_username: str | None = None
        temporary_password: str | None = None
        final_path: RemotePath | None = None
        temp_path: RemotePath | None = None
        with tempfile.TemporaryDirectory(prefix=f"nasmove-{label}-") as temporary:
            source = Path(temporary).resolve() / f"{label}.bin"
            with source.open("wb") as stream:
                stream.write(b"NasMove controlled fault probe\n")
                stream.truncate(96 * 1024 * 1024)
            repository = SqliteTaskRepository(Path(temporary).resolve() / "state.sqlite3")
            config = self._direct_config(f"synology-{label}-probe")
            try:
                connection_password = self.password
                if not mount_tmpfs:
                    temporary_username = f"nasmove_test_{uuid.uuid4().hex[:12]}"
                    temporary_password = secrets.token_urlsafe(24)
                    admin.create_test_user(temporary_username, temporary_password)
                    config = ConnectionConfig(
                        profile_id=config.profile_id,
                        display_name=config.display_name,
                        host=config.host,
                        port=config.port,
                        share=config.share,
                        username=temporary_username,
                        require_encryption=config.require_encryption,
                    )
                    connection_password = temporary_password
                gateway.connect(config, connection_password)
                gateway.make_dir(target_root)
                planned = TaskPlanner(PosixLocalFileGateway(), gateway, repository).plan(
                    PlanRequest(
                        name=f"Synology {label} probe",
                        connection=config,
                        sources=(source,),
                        target_root=target_root,
                        action=TransferAction.COPY,
                        conflict_policy=ConflictPolicy.AUTO_RENAME,
                        verification_policy=VerificationPolicy.FULL,
                    )
                )
                repository.transition_task(
                    planned.task.id,
                    TaskState.PREFLIGHT,
                    TaskState.QUEUED,
                )
                item = repository.list_items(planned.task.id)[0]
                final_path, temp_path = item.final_path, item.temp_path
                gateway.reset_connection()
                if mount_tmpfs:
                    admin.child_command(
                        child,
                        'mount -t tmpfs -o size=32m nasmove-fault "$1" && chmod 0777 "$1"',
                    )
                    mounted = True
                else:
                    if temporary_username is None:
                        raise AssertionError("temporary permission-test user was not created")
                    admin.revoke_test_user(temporary_username)

                runner = RetryingTaskRunner(
                    repository,
                    _FixedCredentialStore(connection_password),
                    self._engine_factory(repository, gateway),
                    sleeper=lambda _delay: None,
                    jitter=lambda: 0.0,
                )
                result = runner.run_task(planned.task.id, CancellationToken())
                category = (
                    "unknown"
                    if result.error is None
                    else map_smb_error(result.error).category.value
                )
                error_code = "none" if result.error is None else redacted_error_code(result.error)
                error_type = "None" if result.error is None else type(result.error).__name__
                final_exists = False
                with suppress(Exception):
                    final_exists = gateway.stat(item.final_path) is not None
                return FaultScenarioResult(
                    category,
                    error_code,
                    error_type,
                    source.exists(),
                    final_exists,
                )
            finally:
                gateway.reset_connection()
                if mounted:
                    with suppress(Exception):
                        admin.child_command(child, 'umount "$1"')
                with suppress(Exception):
                    gateway.connect(self._direct_config(f"synology-{label}-cleanup"), self.password)
                    self._cleanup_remote(gateway, final_path, temp_path)
                gateway.disconnect()
                with suppress(Exception):
                    admin.child_command(child, 'rmdir "$1"')
                if temporary_username is not None:
                    with suppress(Exception):
                        admin.delete_test_user(temporary_username)
                repository.close()

    def _direct_config(self, profile: str) -> ConnectionConfig:
        return ConnectionConfig(
            profile_id=ConnectionProfileId(profile),
            display_name=profile,
            host=self.host,
            port=self.port,
            share=self.share,
            username=self.username,
            require_encryption=True,
        )

    @staticmethod
    def _plan_single(
        repository: SqliteTaskRepository,
        gateway: SmbProtocolGateway,
        config: ConnectionConfig,
        source: Path,
        name: str,
    ) -> object:
        planned = TaskPlanner(PosixLocalFileGateway(), gateway, repository).plan(
            PlanRequest(
                name=name,
                connection=config,
                sources=(source,),
                target_root=RemotePath(os.environ["NASMOVE_SYNOLOGY_TEST_ROOT"].strip("/")),
                action=TransferAction.COPY,
                conflict_policy=ConflictPolicy.AUTO_RENAME,
                verification_policy=VerificationPolicy.FULL,
            )
        )
        repository.transition_task(
            planned.task.id,
            TaskState.PREFLIGHT,
            TaskState.QUEUED,
        )
        return planned

    def _run_disconnect_scenario(
        self,
        source: Path,
        database_path: Path,
        proxy: LocalTcpFaultProxy,
        percentages: tuple[int, ...],
    ) -> SynologyResult:
        control = FaultProxy(proxy.control_endpoint)
        gateway = SmbProtocolGateway(auth_protocol="ntlm")
        repository = SqliteTaskRepository(database_path)
        final_path: RemotePath | None = None
        temp_path: RemotePath | None = None
        try:
            config = ConnectionConfig(
                profile_id=ConnectionProfileId("synology-fault-probe"),
                display_name="Synology fault probe",
                host="127.0.0.1",
                port=proxy.data_port,
                share=self.share,
                username=self.username,
                require_encryption=True,
            )
            gateway.connect(config, self.password)
            planned = TaskPlanner(PosixLocalFileGateway(), gateway, repository).plan(
                PlanRequest(
                    name="Synology disconnect probe",
                    connection=config,
                    sources=(source,),
                    target_root=RemotePath(self.test_root),
                    action=TransferAction.COPY,
                    conflict_policy=ConflictPolicy.AUTO_RENAME,
                    verification_policy=VerificationPolicy.FULL,
                )
            )
            repository.transition_task(
                planned.task.id,
                TaskState.PREFLIGHT,
                TaskState.QUEUED,
            )
            item = repository.list_items(planned.task.id)[0]
            final_path = item.final_path
            temp_path = item.temp_path
            gateway.reset_connection()
            maximum_replay = [0]
            triggered = 0

            def inject_disconnects() -> None:
                nonlocal triggered
                previous_percentage = 0
                for percentage in percentages:
                    delta = percentage - previous_percentage
                    threshold = max(4 * 1024 * 1024, source.stat().st_size * delta // 100)
                    if not proxy.wait_for_additional_upstream_bytes(threshold, timeout=600):
                        return
                    control.disconnect()
                    triggered += 1
                    previous_percentage = percentage

            def engine_factory(task: TaskRecord, password: str) -> TransferEngine:
                gateway.connect(task.connection, password)
                current = repository.get_item(item.id)
                remote = gateway.stat(current.temp_path)
                if remote is not None:
                    maximum_replay[0] = max(
                        maximum_replay[0],
                        max(0, remote.size - current.confirmed_offset),
                    )
                return self._build_engine(
                    repository, password, gateway_factory=_ntlm_gateway
                )

            injector = threading.Thread(target=inject_disconnects, daemon=True)
            injector.start()
            runner = RetryingTaskRunner(
                repository,
                _FixedCredentialStore(self.password),
                engine_factory,
                sleeper=lambda _delay: None,
                jitter=lambda: 0.0,
            )
            result = runner.run_task(planned.task.id, CancellationToken())
            injector.join(timeout=2)
            if triggered != len(percentages):
                raise AssertionError(
                    f"fault proxy injected {triggered} of {len(percentages)} disconnects"
                )
            if not result.success:
                raise AssertionError(f"transfer did not recover: {result.state.value}")
            completed = repository.get_item(item.id)
            final_path = completed.final_path
            with source.open("rb") as local_stream:
                source_hash = sha256_stream(local_stream).hexdigest
            with gateway.open_read(completed.final_path) as remote_stream:
                target_hash = sha256_stream(remote_stream).hexdigest
            source_exists = source.exists()
            gateway.remove_file(completed.final_path)
            final_path = None
            return SynologyResult(
                source_hash,
                target_hash,
                source_exists,
                maximum_replay[0],
                disconnects=len(percentages),
            )
        finally:
            self._cleanup_remote(gateway, final_path, temp_path)
            gateway.disconnect()
            repository.close()

    def _engine_factory(
        self,
        repository: SqliteTaskRepository,
        gateway: SmbProtocolGateway,
        *,
        gateway_factory: Callable[[], object] = SmbProtocolGateway,
    ) -> Callable[[TaskRecord, str], TransferEngine]:
        """Build an engine factory that connects its own session per attempt.

        The outer ``gateway`` is reconnected once per attempt because callers
        use it to measure replay and to read results after the run; the engine
        itself hands every item worker its own gateway and session.
        """

        def factory(task: TaskRecord, password: str) -> TransferEngine:
            gateway.connect(task.connection, password)
            return self._build_engine(
                repository, password, gateway_factory=gateway_factory
            )

        return factory

    def _build_engine(
        self,
        repository: SqliteTaskRepository,
        password: str,
        *,
        gateway_factory: Callable[[], object] = SmbProtocolGateway,
    ) -> TransferEngine:
        """Build the production-equivalent concurrent engine for one attempt.

        Mirrors ``nasmove.ui.desktop_app.build_engine``: the engine is created
        with a per-item worker factory, so every item worker owns a freshly
        connected gateway, its own session and its own SQLite thread
        connection.  This is the concurrent path the application ships, so the
        isolated NAS gate exercises production behaviour rather than the serial
        compatibility branch.
        """
        local = PosixLocalFileGateway()
        events = _WorkerEventSink()

        def build_item_worker(
            worker_task: TaskRecord, worker_password: str
        ) -> TransferItemWorker:
            gateway = gateway_factory()
            try:
                session = gateway.connect(worker_task.connection, worker_password)
                verifier = IntegrityVerifier(repository, local, gateway, session)
                worker = TransferItemWorker(
                    repository,
                    RecoveryCoordinator(repository, local, gateway, session),
                    CheckpointWriter(repository, local, gateway),
                    verifier,
                    TargetCommitter(repository, gateway, session),
                    SourceDeletionService(repository, local, gateway, verifier),
                    smb_gateway=gateway,
                    session=session,
                    event_sink=events,
                )
            except BaseException:
                # The engine classifies the failure, but this factory still owns
                # the resources it created on the worker thread.
                with suppress(Exception):
                    gateway.disconnect()
                with suppress(Exception):
                    repository.release_thread_connection()
                raise
            self._record_worker_resources(gateway, session)
            return worker

        return TransferEngine(
            repository,
            item_worker_factory=build_item_worker,
            password=password,
        )

    def _reset_worker_records(self) -> None:
        with self._worker_records_lock:
            self._worker_gateways.clear()
            self._worker_sessions.clear()
            self._worker_thread_ids.clear()

    def _record_worker_resources(self, gateway: object, session: SessionInfo) -> None:
        if not self._record_workers:
            return
        with self._worker_records_lock:
            self._worker_gateways.append(gateway)
            self._worker_sessions.append(session)
            self._worker_thread_ids.append(threading.get_ident())

    def _worker_resource_evidence(self) -> tuple[int, int, int]:
        with self._worker_records_lock:
            gateways = len({id(entry) for entry in self._worker_gateways})
            sessions = len({id(entry) for entry in self._worker_sessions})
            threads = len(set(self._worker_thread_ids))
        return gateways, sessions, threads

    @staticmethod
    def _cleanup_remote(
        gateway: SmbProtocolGateway,
        final_path: RemotePath | None,
        temp_path: RemotePath | None,
    ) -> None:
        for path in (final_path, temp_path):
            if path is None:
                continue
            with suppress(Exception):
                if gateway.stat(path) is not None:
                    gateway.remove_file(path)

    @staticmethod
    def _transfer_size(*, size_gib: int | None, size_mib: int | None) -> int:
        if (size_gib is None) == (size_mib is None):
            raise ValueError("provide exactly one transfer size")
        value = size_gib if size_gib is not None else size_mib
        if type(value) is not int or value <= 0:
            raise ValueError("transfer size must be a positive integer")
        multiplier = 1024**3 if size_gib is not None else 1024**2
        return value * multiplier

    def _wait_for_port(self, *, unreachable: bool, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((self.host, self.port), timeout=1):
                    reachable = True
            except OSError:
                reachable = False
            if reachable is not unreachable:
                return True
            time.sleep(0.5)
        return False

    @staticmethod
    def _validate_percentages(values: list[int]) -> tuple[int, ...]:
        percentages = tuple(values)
        if not percentages or any(type(value) is not int for value in percentages):
            raise ValueError("disconnect percentages must be integers")
        if tuple(sorted(set(percentages))) != percentages:
            raise ValueError("disconnect percentages must be unique and increasing")
        if percentages[0] <= 0 or percentages[-1] >= 100:
            raise ValueError("disconnect percentages must be between 1 and 99")
        return percentages


@pytest.fixture
def synology_fixture() -> SynologyFixture:
    if os.environ.get("NASMOVE_TEST_SYNOLOGY") != "1":
        pytest.skip("set NASMOVE_TEST_SYNOLOGY=1 for the isolated Synology gate")
    if os.environ.get("NASMOVE_TEST_SYNOLOGY_READY") != "1":
        pytest.skip("set NASMOVE_TEST_SYNOLOGY_READY=1 after the test-share checklist")
    return SynologyFixture()
