import pytest

from nasmove.core.errors import ConnectionProfileInUse
from nasmove.core.model import ConnectionProfileId
from nasmove.ui.connection_profile_service import (
    ConnectionProfileService,
    ProfileArchiveResult,
)
from tests.fixtures.builders import build_connection_config


class Repository:
    def __init__(self) -> None:
        self.config = build_connection_config()
        self.archived: list[ConnectionProfileId] = []
        self.block_archive = False

    def list_connection_profiles(self):
        return (self.config,)

    def get_connection_profile(self, profile_id):
        if profile_id != self.config.profile_id:
            raise KeyError(profile_id)
        return self.config

    def archive_connection_profile(self, profile_id):
        if self.block_archive:
            raise ConnectionProfileInUse(str(profile_id))
        self.archived.append(profile_id)


class Credentials:
    def __init__(self) -> None:
        self.deleted: list[ConnectionProfileId] = []
        self.fail = False

    def delete_password(self, profile_id):
        if self.fail:
            raise RuntimeError("secret backend detail")
        self.deleted.append(profile_id)


def test_service_lists_and_reads_profiles() -> None:
    repository = Repository()
    service = ConnectionProfileService(repository, Credentials())

    assert service.list_profiles() == (repository.config,)
    assert service.get_profile(repository.config.profile_id) == repository.config


def test_archive_removes_password_after_database_archive() -> None:
    repository = Repository()
    credentials = Credentials()
    service = ConnectionProfileService(repository, credentials)

    result = service.archive(repository.config.profile_id)

    assert result == ProfileArchiveResult(True, True, None)
    assert repository.archived == [repository.config.profile_id]
    assert credentials.deleted == [repository.config.profile_id]


def test_archive_does_not_touch_password_when_profile_is_in_use() -> None:
    repository = Repository()
    repository.block_archive = True
    credentials = Credentials()
    service = ConnectionProfileService(repository, credentials)

    with pytest.raises(ConnectionProfileInUse):
        service.archive(repository.config.profile_id)

    assert credentials.deleted == []


def test_keychain_failure_keeps_profile_archived_and_returns_safe_warning() -> None:
    repository = Repository()
    credentials = Credentials()
    credentials.fail = True
    service = ConnectionProfileService(repository, credentials)

    result = service.archive(repository.config.profile_id)

    assert result == ProfileArchiveResult(True, False, "credential_cleanup_failed")
    assert repository.archived == [repository.config.profile_id]
