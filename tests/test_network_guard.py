from __future__ import annotations

import socket
import unittest
from unittest import mock
from urllib.parse import urlsplit

from lexsond.probe import (
    UnsafeTargetAddress,
    _create_guarded_http_connection,
    _guarded_socket_connection,
    _validate_resolved_addresses,
)


def _candidate(address: str, port: int = 443) -> tuple[object, ...]:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr: tuple[object, ...]
    if family == socket.AF_INET6:
        sockaddr = (address, port, 0, 0)
    else:
        sockaddr = (address, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


class NetworkGuardTests(unittest.TestCase):
    def test_blocks_private_and_link_local_dns_answers(self) -> None:
        for address in ("10.0.0.8", "169.254.169.254", "fd00::1"):
            with self.subTest(address=address):
                with self.assertRaises(UnsafeTargetAddress):
                    _validate_resolved_addresses("api.example.com", [_candidate(address)])

    def test_blocks_multicast_and_nat64_embedded_protected_addresses(self) -> None:
        for address in (
            "224.0.0.1",
            "ff02::1",
            "64:ff9b::7f00:1",
            "64:ff9b::a9fe:a9fe",
        ):
            with self.subTest(address=address):
                with self.assertRaises(UnsafeTargetAddress):
                    _validate_resolved_addresses("api.example.com", [_candidate(address)])

    def test_allows_nat64_when_the_embedded_ipv4_is_public(self) -> None:
        _validate_resolved_addresses(
            "api.example.com",
            [_candidate("64:ff9b::808:808")],
        )

    def test_blocks_mixed_public_and_private_dns_answers(self) -> None:
        with self.assertRaises(UnsafeTargetAddress):
            _validate_resolved_addresses(
                "api.example.com",
                [_candidate("8.8.8.8"), _candidate("127.0.0.1")],
            )

    def test_allows_only_loopback_for_explicit_loopback_targets(self) -> None:
        _validate_resolved_addresses("127.0.0.1", [_candidate("127.0.0.1")])
        _validate_resolved_addresses("localhost", [_candidate("::1")])
        with self.assertRaises(UnsafeTargetAddress):
            _validate_resolved_addresses("localhost", [_candidate("8.8.8.8")])

    def test_guard_rejects_before_creating_a_socket(self) -> None:
        with (
            mock.patch(
                "lexsond.probe.socket.getaddrinfo",
                return_value=[_candidate("169.254.169.254")],
            ),
            mock.patch("lexsond.probe.socket.socket") as socket_factory,
            self.assertRaises(UnsafeTargetAddress),
        ):
            _guarded_socket_connection(("api.example.com", 443), 1.0)
        socket_factory.assert_not_called()

    def test_connection_keeps_original_host_for_tls_and_uses_guard(self) -> None:
        connection = _create_guarded_http_connection(
            urlsplit("https://api.example.com/v1"),
            2.0,
        )
        self.assertEqual(connection.host, "api.example.com")
        self.assertEqual(connection.port, 443)
        self.assertIs(connection._create_connection, _guarded_socket_connection)


if __name__ == "__main__":
    unittest.main()
