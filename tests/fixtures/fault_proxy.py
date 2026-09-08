from __future__ import annotations

import os
import socket
import socketserver
import threading
import time
from contextlib import AbstractContextManager, suppress
from http.server import BaseHTTPRequestHandler
from types import TracebackType
from typing import Self
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


class _ProxyState:
    def __init__(self) -> None:
        self._latency_ms = 0
        self._upstream_bytes = 0
        self._connections: set[socket.socket] = set()
        self._condition = threading.Condition()

    @property
    def latency_seconds(self) -> float:
        with self._condition:
            return self._latency_ms / 1000

    @property
    def upstream_bytes(self) -> int:
        with self._condition:
            return self._upstream_bytes

    def set_latency(self, milliseconds: int) -> None:
        with self._condition:
            self._latency_ms = milliseconds

    def add(self, connection: socket.socket) -> None:
        with self._condition:
            self._connections.add(connection)

    def discard(self, connection: socket.socket) -> None:
        with self._condition:
            self._connections.discard(connection)

    def record_upstream_bytes(self, byte_count: int) -> None:
        with self._condition:
            self._upstream_bytes += byte_count
            self._condition.notify_all()

    def wait_for_upstream_bytes(self, byte_count: int, timeout: float) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: self._upstream_bytes >= byte_count,
                timeout=timeout,
            )

    def disconnect_all(self) -> None:
        with self._condition:
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass


class _ThreadingTcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _ForwardHandler(socketserver.BaseRequestHandler):
    state: _ProxyState
    upstream_host: str
    upstream_port: int

    def handle(self) -> None:
        client = self.request
        try:
            upstream = socket.create_connection(
                (self.upstream_host, self.upstream_port), timeout=10
            )
        except OSError:
            with suppress(OSError):
                client.shutdown(socket.SHUT_RDWR)
            return
        self.state.add(client)
        self.state.add(upstream)
        try:
            left = threading.Thread(
                target=self._copy,
                args=(client, upstream, True),
                daemon=True,
            )
            right = threading.Thread(
                target=self._copy,
                args=(upstream, client, False),
                daemon=True,
            )
            left.start()
            right.start()
            left.join()
            right.join()
        finally:
            self.state.discard(client)
            self.state.discard(upstream)
            for connection in (client, upstream):
                try:
                    connection.close()
                except OSError:
                    pass

    def _copy(
        self,
        source: socket.socket,
        target: socket.socket,
        count_upstream: bool,
    ) -> None:
        try:
            while True:
                data = source.recv(64 * 1024)
                if not data:
                    return
                latency = self.state.latency_seconds
                if latency:
                    time.sleep(latency)
                target.sendall(data)
                if count_upstream:
                    self.state.record_upstream_bytes(len(data))
        except OSError:
            return
        finally:
            for connection in (source, target):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class _ControlHandler(BaseHTTPRequestHandler):
    state: _ProxyState

    def do_POST(self) -> None:
        if self.path != "/control":
            self.send_error(404)
            return
        command = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        if command == "disconnect":
            self.state.disconnect_all()
        elif command == "restore":
            self.state.set_latency(0)
        elif command.startswith("latency:"):
            try:
                milliseconds = int(command.split(":", 1)[1])
            except ValueError:
                self.send_error(400)
                return
            if milliseconds < 0:
                self.send_error(400)
                return
            self.state.set_latency(milliseconds)
        else:
            self.send_error(400)
            return
        self.send_response(204)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


class LocalTcpFaultProxy(AbstractContextManager["LocalTcpFaultProxy"]):
    """Loopback-only SMB data proxy with a separate HTTP control endpoint."""

    def __init__(self, upstream_host: str, upstream_port: int = 445) -> None:
        self._state = _ProxyState()
        data_handler = type(
            "DataHandler",
            (_ForwardHandler,),
            {
                "state": self._state,
                "upstream_host": upstream_host,
                "upstream_port": upstream_port,
            },
        )
        control_handler = type(
            "ControlHandler",
            (_ControlHandler,),
            {"state": self._state},
        )
        self._data_server = _ThreadingTcpServer(("127.0.0.1", 0), data_handler)
        self._control_server = _ThreadingTcpServer(("127.0.0.1", 0), control_handler)
        self._threads: tuple[threading.Thread, ...] = ()

    @property
    def data_port(self) -> int:
        return int(self._data_server.server_address[1])

    @property
    def control_endpoint(self) -> str:
        return f"http://127.0.0.1:{self._control_server.server_address[1]}"

    def wait_for_upstream_bytes(self, byte_count: int, timeout: float) -> bool:
        return self._state.wait_for_upstream_bytes(byte_count, timeout)

    def wait_for_additional_upstream_bytes(self, byte_count: int, timeout: float) -> bool:
        return self._state.wait_for_upstream_bytes(
            self._state.upstream_bytes + byte_count,
            timeout,
        )

    def __enter__(self) -> Self:
        self._threads = tuple(
            threading.Thread(target=server.serve_forever, daemon=True)
            for server in (self._data_server, self._control_server)
        )
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self._state.disconnect_all()
        for server in (self._data_server, self._control_server):
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=2)
