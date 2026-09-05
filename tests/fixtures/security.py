from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest


@dataclass
class FakeKeyring:
    """In-memory keyring substitute; no real Keychain access is performed."""

    _backend_name: str = "keyring.backends.macOS"
    values: dict[tuple[str, str], str] = field(default_factory=dict)
    calls: list[tuple[str, str, str, str | None]] = field(default_factory=list)

    @property
    def backend_name(self) -> str:
        return self._backend_name

    @backend_name.setter
    def backend_name(self, value: str) -> None:
        self._backend_name = value

    def get_keyring(self) -> Any:
        backend_type = type("FakeBackend", (), {})
        backend_type.__module__ = self._backend_name
        return backend_type()

    def get_password(self, service: str, account: str) -> str | None:
        self.calls.append(("get", service, account, None))
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, password: str) -> None:
        self.calls.append(("set", service, account, password))
        self.values[(service, account)] = password

    def delete_password(self, service: str, account: str) -> None:
        self.calls.append(("delete", service, account, None))
        self.values.pop((service, account), None)


@pytest.fixture
def fake_keyring() -> FakeKeyring:
    return FakeKeyring()
