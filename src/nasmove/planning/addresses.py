from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from nasmove.core.errors import DomainValidationError
from nasmove.planning.paths import normalize_remote_path, validate_remote_component


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
        if (
            parsed.scheme.lower() != "smb"
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise DomainValidationError("invalid SMB address")
        try:
            host = parsed.hostname
            if ":" in parsed.netloc.rsplit("@", 1)[-1] and parsed.netloc.rsplit("@", 1)[-1].endswith(":"):
                raise DomainValidationError("SMB address port must not be empty")
            port = parsed.port if parsed.port is not None else 445
            if not 1 <= port <= 65535:
                raise DomainValidationError("SMB address port must be between 1 and 65535")
        except ValueError as exc:
            raise DomainValidationError("invalid SMB address port") from exc
        if host is None:
            raise DomainValidationError("SMB address host must not be empty")
        path = parsed.path.removeprefix("/")
    else:
        host, separator, path, port = _split_non_url_address(value)
        if not separator:
            raise DomainValidationError("SMB address must include a share")

    components = path.split("/") if path else []
    if not components or not components[0]:
        raise DomainValidationError("SMB address share must not be empty")
    if any(not component for component in components):
        raise DomainValidationError("SMB address contains an empty component")
    share = components[0]
    initial_path = "/".join(components[1:])
    try:
        validate_remote_component(share)
    except ValueError as exc:
        raise DomainValidationError(f"SMB address share is invalid: {exc}") from exc
    _validate_host(host)
    if initial_path:
        initial_path = normalize_remote_path(initial_path).value
    return ParsedSmbAddress(host=host, share=share, initial_path=initial_path, port=port)


def _split_non_url_address(value: str) -> tuple[str, str, str, int]:
    if value.startswith("["):
        closing = value.find("]")
        if closing <= 1 or closing + 1 >= len(value):
            raise DomainValidationError("invalid bracketed IPv6 SMB address")
        suffix = value[closing + 1 :]
        if suffix.startswith("/"):
            return value[1:closing], "/", suffix[1:], 445
        if not suffix.startswith(":"):
            raise DomainValidationError("invalid bracketed IPv6 SMB address")
        port_text, separator, path = suffix[1:].partition("/")
        return value[1:closing], separator, path, _parse_port(port_text)
    host_port, separator, path = value.partition("/")
    host, port = _split_host_port(host_port)
    return host, separator, path, port


def _split_host_port(value: str) -> tuple[str, int]:
    if ":" not in value:
        return value, 445
    if value.count(":") > 1:
        raise DomainValidationError("IPv6 SMB addresses must use brackets")
    host, port_text = value.rsplit(":", 1)
    return host, _parse_port(port_text)


def _parse_port(value: str) -> int:
    if not value or not value.isdecimal():
        raise DomainValidationError("SMB address port must be a decimal number")
    port = int(value)
    if not 1 <= port <= 65535:
        raise DomainValidationError("SMB address port must be between 1 and 65535")
    return port


def _validate_host(host: str) -> None:
    if not host or any(ord(char) < 32 for char in host) or "/" in host or "\\" in host:
        raise DomainValidationError("SMB address host is invalid")
