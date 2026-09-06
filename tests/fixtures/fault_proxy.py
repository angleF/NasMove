from __future__ import annotations

import os
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class FaultProxy:
    """Control client restricted to a loopback fault proxy."""

    def __init__(self, endpoint: str | None = None) -> None:
        value = endpoint or os.environ.get("NASMOVE_FAULT_PROXY", "")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("NASMOVE_FAULT_PROXY must be an HTTP(S) endpoint")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("fault proxy must be local-only")
        self._endpoint = value.rstrip("/")

    def _control(self, command: str) -> None:
        request = Request(f"{self._endpoint}/control", data=command.encode(), method="POST")
        with urlopen(request, timeout=5):
            return

    def disconnect(self) -> None:
        self._control("disconnect")

    def add_latency(self, milliseconds: int) -> None:
        if type(milliseconds) is not int or milliseconds < 0:
            raise ValueError("latency must be a non-negative integer")
        self._control(f"latency:{milliseconds}")

    def restore(self) -> None:
        self._control("restore")
