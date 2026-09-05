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
    backend: Any = field(init=False)

    def __post_init__(self) -> None:
        self._replace_backend()

    @property
    def backend_name(self) -> str:
        return self._backend_name

    @backend_name.setter
    def backend_name(self, value: str) -> None:
        self._backend_name = value
        self._replace_backend()

    def _replace_backend(self) -> None:
        owner = self
        backend_type = type(
            "FakeBackend",
            (),
            {
                "__module__": self._backend_name,
                "get_password": lambda backend, service, account: owner._get_password(service, account),
                "set_password": lambda backend, service, account, password: owner._set_password(
                    service, account, password
                ),
                "delete_password": lambda backend, service, account: owner._delete_password(service, account),
            },
        )
        self.backend = backend_type()

    def get_keyring(self) -> Any:
        return self.backend

    def _get_password(self, service: str, account: str) -> str | None:
        self.calls.append(("get", service, account, None))
        return self.values.get((service, account))

    def _set_password(self, service: str, account: str, password: str) -> None:
        self.calls.append(("set", service, account, password))
        self.values[(service, account)] = password

    def _delete_password(self, service: str, account: str) -> None:
        self.calls.append(("delete", service, account, None))
        self.values.pop((service, account), None)


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    import keyring.backends.macOS

    fake = FakeKeyring()
    monkeypatch.setattr(keyring.backends.macOS, "Keyring", type(fake.backend))
    return fake
