from __future__ import annotations

import socket
import socketserver
import threading

from tests.fixtures.fault_proxy import FaultProxy, LocalTcpFaultProxy


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        while data := self.request.recv(4096):
            self.request.sendall(data)


class _EchoServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


class _CloseAfterReadHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.recv(4096)


def test_local_proxy_forwards_and_disconnects_active_connection() -> None:
    with _EchoServer(("127.0.0.1", 0), _EchoHandler) as upstream:
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        with LocalTcpFaultProxy("127.0.0.1", upstream.server_address[1]) as server:
            client = FaultProxy(server.control_endpoint)
            with socket.create_connection(("127.0.0.1", server.data_port), timeout=2) as stream:
                stream.settimeout(2)
                stream.sendall(b"before")
                assert stream.recv(6) == b"before"
                assert server.wait_for_upstream_bytes(6, timeout=2) is True
                assert server.wait_for_additional_upstream_bytes(1, timeout=0.01) is False

                client.disconnect()

                assert stream.recv(1) == b""

            client.restore()
            with socket.create_connection(("127.0.0.1", server.data_port), timeout=2) as stream:
                stream.settimeout(2)
                stream.sendall(b"after")
                assert stream.recv(5) == b"after"

        upstream.shutdown()


def test_local_proxy_propagates_unsolicited_upstream_close() -> None:
    with _EchoServer(("127.0.0.1", 0), _CloseAfterReadHandler) as upstream:
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        with (
            LocalTcpFaultProxy("127.0.0.1", upstream.server_address[1]) as server,
            socket.create_connection(("127.0.0.1", server.data_port), timeout=2) as stream,
        ):
            stream.settimeout(2)
            stream.sendall(b"close upstream")

            assert stream.recv(1) == b""

        upstream.shutdown()
