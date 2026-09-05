from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from nasmove.core.errors import DomainValidationError
from nasmove.planning.paths import normalize_remote_path


@dataclass(frozen=True, slots=True)
class ParsedSmbAddress:
    host: str
    share: str
    initial_path: str = ""
    port: int = 445


def parse_smb_address(value: str) -> ParsedSmbAddress:
    """Parse an SMB URL or a host/share/path address without touching the network."""
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise DomainValidationError("SMB address must be a non-empty string")
    if "\\" in value or any(char.isspace() for char in value):
        raise DomainValidationError("SMB address must use slash separators and contain no whitespace")

    if value.lower().startswith("smb://"):
        parsed = urlsplit(value)
        if parsed.scheme.lower() != "smb" or not parsed.netloc or parsed.query or parsed.fragment:
            raise DomainValidationError("invalid SMB address")
        try:
            host = parsed.hostname
            port = parsed.port or 445
        except ValueError as exc:
            raise DomainValidationError("invalid SMB address port") from exc
        if host is None:
            raise DomainValidationError("SMB address host must not be empty")
        path = parsed.path.removeprefix("/")
    else:
        host, separator, path = _split_non_url_address(value)
        if not separator:
            raise DomainValidationError("SMB address must include a share")
        port = 445

    components = path.split("/") if path else []
    if not components or not components[0]:
        raise DomainValidationError("SMB address share must not be empty")
    if any(not component for component in components):
        raise DomainValidationError("SMB address contains an empty component")
    share = components[0]
    initial_path = "/".join(components[1:])
    _validate_component(share, "share")
    _validate_host(host)
    if initial_path:
        initial_path = normalize_remote_path(initial_path).value
    return ParsedSmbAddress(host=host, share=share, initial_path=initial_path, port=port)


def _split_non_url_address(value: str) -> tuple[str, str, str]:
    if value.startswith("["):
        closing = value.find("]")
        if closing <= 1 or closing + 1 >= len(value) or value[closing + 1] != "/":
            raise DomainValidationError("invalid bracketed IPv6 SMB address")
        return value[1:closing], "/", value[closing + 2 :]
    host, separator, path = value.partition("/")
    return host, separator, path


def _validate_host(host: str) -> None:
    if not host or any(ord(char) < 32 for char in host) or "/" in host or "\\" in host:
        raise DomainValidationError("SMB address host is invalid")


def _validate_component(component: str, label: str) -> None:
    if component in {".", ".."} or any(ord(char) < 32 for char in component):
        raise DomainValidationError(f"SMB address {label} is invalid")
