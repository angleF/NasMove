from __future__ import annotations

import pytest

from nasmove.core.errors import UnsafeCredentialBackend
from nasmove.core.model import ConnectionProfileId
from nasmove.security.keychain_store import MacOSKeychainCredentialStore


def test_non_macos_keyring_backend_is_rejected(fake_keyring) -> None:
    fake_keyring.backend_name = "keyrings.alt.file.PlaintextKeyring"
    with pytest.raises(UnsafeCredentialBackend):
        MacOSKeychainCredentialStore(fake_keyring)


def test_get_set_delete_use_fixed_service_and_profile_account(fake_keyring) -> None:
    store = MacOSKeychainCredentialStore(fake_keyring)
    profile_id = ConnectionProfileId("profile-1")

    store.set_password(profile_id, "密码🔐")
    assert store.get_password(profile_id) == "密码🔐"
    store.delete_password(profile_id)
    assert store.get_password(profile_id) is None

    assert fake_keyring.calls == [
        ("set", "com.nasmove.smb", "profile-1", "密码🔐"),
        ("get", "com.nasmove.smb", "profile-1", None),
        ("delete", "com.nasmove.smb", "profile-1", None),
        ("get", "com.nasmove.smb", "profile-1", None),
    ]


def test_backend_instance_remains_pinned_after_global_backend_change(fake_keyring) -> None:
    store = MacOSKeychainCredentialStore(fake_keyring)
    fake_keyring.backend_name = "keyrings.alt.file.PlaintextKeyring"
    store.set_password(ConnectionProfileId("profile-1"), "secret")
    assert fake_keyring.values[("com.nasmove.smb", "profile-1")] == "secret"


def test_backend_failure_is_wrapped_without_secret(fake_keyring) -> None:
    store = MacOSKeychainCredentialStore(fake_keyring)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("password=非常秘密")

    fake_keyring.backend.get_password = fail  # type: ignore[method-assign]
    with pytest.raises(UnsafeCredentialBackend) as caught:
        store.get_password(ConnectionProfileId("profile-1"))
    assert "非常秘密" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_keyring_import_failure_is_wrapped(monkeypatch) -> None:
    import builtins

    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "keyring":
            raise ImportError("password=hidden")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(UnsafeCredentialBackend) as caught:
        MacOSKeychainCredentialStore()
    assert "hidden" not in str(caught.value)


def test_backend_instance_is_pinned_against_keyring_switch(fake_keyring) -> None:
    store = MacOSKeychainCredentialStore(fake_keyring)
    fake_keyring.backend_name = "keyrings.alt.file.PlaintextKeyring"
    store.set_password(ConnectionProfileId("profile-1"), "secret")
    assert fake_keyring.values[("com.nasmove.smb", "profile-1")] == "secret"
