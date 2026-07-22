from __future__ import annotations

import hashlib
import unittest

from lexsond.web.auth import (
    AuthConfiguration,
    AuthMode,
    PasswordManager,
    coarse_ip_prefix,
    issue_one_time_secret,
    normalize_email,
    safe_return_to,
)


class AuthSecurityTests(unittest.TestCase):
    def test_required_is_default_and_secure_cookie_defaults_on(self) -> None:
        config = AuthConfiguration.from_values(auth_mode=None, listen_host="0.0.0.0")

        self.assertEqual(config.mode, AuthMode.REQUIRED)
        self.assertTrue(config.cookie_secure)

    def test_local_single_user_requires_numeric_loopback(self) -> None:
        for host in ("127.0.0.1", "::1"):
            config = AuthConfiguration.from_values(
                auth_mode="local-single-user", listen_host=host
            )
            self.assertEqual(config.mode, AuthMode.LOCAL_SINGLE_USER)
            self.assertFalse(config.cookie_secure)

        for host in ("0.0.0.0", "::", "localhost", "192.168.1.10", "example.com"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                AuthConfiguration.from_values(
                    auth_mode="local-single-user", listen_host=host
                )

    def test_auth_mode_and_cookie_flag_reject_unknown_values(self) -> None:
        with self.assertRaises(ValueError):
            AuthConfiguration.from_values(auth_mode="disabled", listen_host="127.0.0.1")
        with self.assertRaises(ValueError):
            AuthConfiguration.from_values(
                auth_mode="required",
                listen_host="127.0.0.1",
                cookie_secure="sometimes",
            )

    def test_one_time_secret_is_256_bit_hashed_and_repr_safe(self) -> None:
        first, first_hash = issue_one_time_secret()
        second, second_hash = issue_one_time_secret()

        self.assertEqual(len(first_hash), 32)
        self.assertEqual(len(second_hash), 32)
        self.assertNotEqual(first_hash, second_hash)
        self.assertEqual(repr(first), "OneTimeSecret('[REDACTED]')")
        raw = first.consume()
        self.assertEqual(hashlib.sha256(raw.encode("ascii")).digest(), first_hash)
        self.assertNotIn(raw, repr(first))
        self.assertGreaterEqual(len(raw), 43)
        with self.assertRaises(RuntimeError):
            first.consume()

    def test_email_ip_and_return_path_are_canonicalized(self) -> None:
        self.assertEqual(normalize_email("  Person@Example.COM "), "person@example.com")
        for invalid in ("", "person", "a@", "@example.com", "a\n@example.com"):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                normalize_email(invalid)

        self.assertEqual(coarse_ip_prefix("203.0.113.42"), "203.0.113.0/24")
        self.assertEqual(coarse_ip_prefix("2001:db8:abcd:1234::1"), "2001:db8:abcd:1200::/56")
        self.assertIsNone(coarse_ip_prefix(None))

        self.assertEqual(safe_return_to("/probes/single?tab=history"), "/probes/single?tab=history")
        for unsafe in (None, "", "//evil.example", "https://evil.example", "/\\evil", "/a\nb"):
            with self.subTest(value=unsafe):
                self.assertEqual(safe_return_to(unsafe), "/overview")

    def test_argon2id_password_contract_when_dependency_is_installed(self) -> None:
        try:
            manager = PasswordManager()
        except ModuleNotFoundError:
            self.skipTest("install the pinned web authentication dependency")

        password = "correct horse battery staple"
        encoded = manager.hash(password)
        self.assertTrue(encoded.startswith("$argon2id$"))
        self.assertTrue(manager.verify(password, encoded))
        self.assertFalse(manager.verify("wrong password value", encoded))
        with self.assertRaises(ValueError):
            manager.hash("too-short")


if __name__ == "__main__":
    unittest.main()
