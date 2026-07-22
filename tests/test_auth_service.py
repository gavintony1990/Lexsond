from __future__ import annotations

import hashlib
import unittest
from datetime import UTC, datetime
from typing import Any

from lexsond.web.auth import secret_matches
from lexsond.web.auth_service import (
    AuthRateLimited,
    AuthRejected,
    AuthenticationService,
)


class FakePasswords:
    def __init__(self) -> None:
        self.verify_calls: list[tuple[str, str]] = []

    def hash(self, password: str) -> str:
        if len(password) < 12:
            raise ValueError("password must contain at least 12 characters")
        return f"encoded::{password}"

    def verify(self, password: str, encoded_hash: str) -> bool:
        self.verify_calls.append((password, encoded_hash))
        return encoded_hash == f"encoded::{password}"

    def needs_rehash(self, encoded_hash: str) -> bool:
        return encoded_hash.startswith("legacy::")


class FakeAuthStore:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.registration: dict[str, Any] | None = None
        self.created_session: dict[str, Any] | None = None
        self.audits: list[dict[str, Any]] = []
        self.verified_hash: bytes | None = None
        self.principal: dict[str, Any] | None = None
        self.rotated_csrf_hash: bytes | None = None
        self.revoked_token_hash: bytes | None = None
        self.sessions: list[dict[str, Any]] = []
        self.action_tokens: dict[bytes, dict[str, Any]] = {}
        self.last_action_token: dict[str, Any] | None = None
        self.rate_limit_allowed = True
        self.rate_limit_calls: list[dict[str, Any]] = []

    def register_user(self, **value: Any) -> dict[str, Any]:
        self.registration = value
        user = {
            "user_id": "00000000-0000-4000-8000-000000000101",
            "email": value["email_display"],
            "email_normalized": value["email_normalized"],
            "password_hash": value["password_hash"],
            "display_name": value["display_name"],
            "status": "PENDING_VERIFICATION",
            "system_role": "USER",
            "workspace_id": "00000000-0000-4000-8000-000000000102",
            "workspace_role": "OWNER",
        }
        self.users[value["email_normalized"]] = user
        return {key: item for key, item in user.items() if key != "password_hash"}

    def find_login_user(self, email_normalized: str) -> dict[str, Any] | None:
        return self.users.get(email_normalized)

    def update_password_hash(self, user_id: str, password_hash: str) -> None:
        for user in self.users.values():
            if user["user_id"] == user_id:
                user["password_hash"] = password_hash

    def create_session(self, **value: Any) -> dict[str, Any]:
        self.created_session = value
        return {
            "session_id": "00000000-0000-4000-8000-000000000103",
            "user_id": value["user_id"],
            "workspace_id": value["workspace_id"],
            "created_at": value["now"].isoformat(),
            "idle_expires_at": value["idle_expires_at"].isoformat(),
            "absolute_expires_at": value["absolute_expires_at"].isoformat(),
        }

    def consume_email_verification(self, *, token_hash: bytes, now: datetime) -> dict[str, Any]:
        del now
        self.verified_hash = token_hash
        return {"status": "ACTIVE", "email_verified": True}

    def create_email_verification(self, **value: Any) -> dict[str, Any]:
        self.last_action_token = value
        user = next(
            item for item in self.users.values() if item["user_id"] == value["user_id"]
        )
        if user["status"] != "PENDING_VERIFICATION":
            raise AuthRejected("邮箱已经验证")
        self.action_tokens[value["token_hash"]] = {
            **value,
            "purpose": "verify_email",
        }
        return {"user_id": user["user_id"], "email": user["email"]}

    def create_password_reset(self, **value: Any) -> dict[str, Any] | None:
        user = self.users.get(value["email_normalized"])
        if user is None or user["status"] not in {"PENDING_VERIFICATION", "ACTIVE"}:
            return None
        self.last_action_token = value
        self.action_tokens[value["token_hash"]] = {
            **value,
            "purpose": "reset_password",
            "user_id": user["user_id"],
        }
        return {"user_id": user["user_id"], "email": user["email"]}

    def consume_password_reset(self, **value: Any) -> dict[str, Any]:
        token = self.action_tokens.get(value["token_hash"])
        if (
            token is None
            or token["purpose"] != "reset_password"
            or token["expires_at"] <= value["now"]
        ):
            raise AuthRejected("重置链接无效或已过期")
        user = next(
            item for item in self.users.values() if item["user_id"] == token["user_id"]
        )
        user["password_hash"] = value["password_hash"]
        self.revoke_all_sessions(user_id=user["user_id"], now=value["now"])
        self.action_tokens.pop(value["token_hash"])
        return {"user_id": user["user_id"], "status": user["status"]}

    def change_password(self, **value: Any) -> int:
        user = next(
            item for item in self.users.values() if item["user_id"] == value["user_id"]
        )
        user["password_hash"] = value["password_hash"]
        return self.revoke_all_sessions(user_id=value["user_id"], now=value["now"])

    def consume_auth_rate_limit(self, **value: Any) -> bool:
        self.rate_limit_calls.append(value)
        return self.rate_limit_allowed

    def resolve_session(self, *, token_hash: bytes, now: datetime) -> dict[str, Any] | None:
        del token_hash, now
        return self.principal

    def rotate_csrf(self, *, session_id: str, csrf_secret_hash: bytes) -> None:
        del session_id
        self.rotated_csrf_hash = csrf_secret_hash

    def revoke_session_by_token(self, *, token_hash: bytes, now: datetime) -> None:
        del now
        self.revoked_token_hash = token_hash

    def list_user_sessions(self, *, user_id: str, limit: int = 50):
        return [value for value in self.sessions if value["user_id"] == user_id][:limit]

    def revoke_user_session(self, *, user_id: str, session_id: str, now: datetime):
        del now
        for value in self.sessions:
            if value["user_id"] == user_id and value["session_id"] == session_id:
                value["revoked_at"] = "now"
                return
        raise RuntimeError("session was not found")

    def revoke_all_sessions(self, *, user_id: str, now: datetime):
        del now
        count = 0
        for value in self.sessions:
            if value["user_id"] == user_id and value.get("revoked_at") is None:
                value["revoked_at"] = "now"
                count += 1
        return count

    def record_auth_audit(self, **value: Any) -> None:
        self.audits.append(value)


class AuthenticationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeAuthStore()
        self.passwords = FakePasswords()
        self.service = AuthenticationService(
            store=self.store,
            password_manager=self.passwords,
            dummy_password_hash="encoded::dummy password value",
        )

    def test_registration_normalizes_email_and_only_store_hashes(self) -> None:
        result = self.service.register(
            email=" Person@Example.COM ",
            password="a long password value",
            display_name="Person",
            ip_address="203.0.113.19",
            user_agent="Browser/1",
        )

        self.assertEqual(result.user["email_normalized"], "person@example.com")
        self.assertEqual(self.store.registration["password_hash"], "encoded::a long password value")
        self.assertNotIn("a long password value", repr(result))
        raw_token = result.verification_secret.consume()
        self.assertEqual(
            self.store.registration["verification_token_hash"],
            hashlib.sha256(raw_token.encode("ascii")).digest(),
        )
        self.assertEqual(self.store.audits[-1]["category"], "REGISTER")

    def test_unknown_email_and_wrong_password_have_one_public_failure(self) -> None:
        for email, password in (
            ("missing@example.com", "wrong password value"),
            ("person@example.com", "wrong password value"),
        ):
            if email == "person@example.com":
                self.service.register(
                    email=email,
                    password="correct password value",
                    display_name="Person",
                )
            with self.subTest(email=email), self.assertRaisesRegex(
                AuthRejected, "邮箱或密码错误"
            ):
                self.service.login(email=email, password=password)

        self.assertIn(
            ("wrong password value", "encoded::dummy password value"),
            self.passwords.verify_calls,
        )
        self.assertEqual(
            [audit["outcome"] for audit in self.store.audits if audit["category"] == "LOGIN"],
            ["INVALID_CREDENTIALS", "INVALID_CREDENTIALS"],
        )

    def test_login_returns_one_use_session_and_csrf_without_storing_raw_values(self) -> None:
        self.service.register(
            email="person@example.com",
            password="correct password value",
            display_name="Person",
        )
        now = datetime(2026, 7, 22, 0, 0, tzinfo=UTC)
        grant = self.service.login(
            email="person@example.com",
            password="correct password value",
            ip_address="203.0.113.19",
            user_agent="Browser/1",
            now=now,
        )

        session_raw = grant.session_secret.consume()
        csrf_raw = grant.csrf_secret.consume()
        self.assertTrue(secret_matches(session_raw, self.store.created_session["token_hash"]))
        self.assertTrue(secret_matches(csrf_raw, self.store.created_session["csrf_secret_hash"]))
        self.assertNotIn(session_raw, repr(grant))
        self.assertNotIn(csrf_raw, repr(grant))
        self.assertEqual(grant.user["status"], "PENDING_VERIFICATION")
        self.assertEqual(
            int((self.store.created_session["idle_expires_at"] - now).total_seconds()),
            12 * 60 * 60,
        )

    def test_verification_consumes_only_the_token_hash(self) -> None:
        result = self.service.register(
            email="person@example.com",
            password="correct password value",
            display_name="Person",
        )
        raw_token = result.verification_secret.consume()
        activated = self.service.verify_email(raw_token)

        self.assertEqual(activated["status"], "ACTIVE")
        self.assertEqual(
            self.store.verified_hash, hashlib.sha256(raw_token.encode("ascii")).digest()
        )

    def test_session_resume_rotates_memory_only_csrf_and_logout_hashes_cookie(self) -> None:
        cookie_value = "session-cookie-value-with-sufficient-entropy"
        self.store.principal = {
            "session_id": "00000000-0000-4000-8000-000000000103",
            "user_id": "00000000-0000-4000-8000-000000000101",
            "workspace_id": "00000000-0000-4000-8000-000000000102",
            "status": "ACTIVE",
            "csrf_secret_hash": b"x" * 32,
        }

        resumed = self.service.resume_session(cookie_value)
        csrf_raw = resumed.csrf_secret.consume()
        self.assertTrue(secret_matches(csrf_raw, self.store.rotated_csrf_hash))
        self.assertNotIn("csrf_secret_hash", resumed.principal)
        self.assertNotIn(csrf_raw, repr(resumed))

        self.service.logout(cookie_value)
        self.assertEqual(
            self.store.revoked_token_hash,
            hashlib.sha256(cookie_value.encode("ascii")).digest(),
        )

    def test_missing_or_invalid_session_is_rejected_without_reflecting_cookie(self) -> None:
        with self.assertRaisesRegex(AuthRejected, "请先登录"):
            self.service.resume_session(None)
        with self.assertRaisesRegex(AuthRejected, "请先登录"):
            self.service.resume_session("not-accepted")

    def test_session_listing_revoke_and_logout_all_are_user_scoped(self) -> None:
        principal = {
            "session_id": "00000000-0000-4000-8000-000000000103",
            "user_id": "00000000-0000-4000-8000-000000000101",
        }
        self.store.sessions = [
            {**principal, "revoked_at": None},
            {
                "session_id": "00000000-0000-4000-8000-000000000104",
                "user_id": principal["user_id"],
                "revoked_at": None,
            },
            {
                "session_id": "00000000-0000-4000-8000-000000000105",
                "user_id": "00000000-0000-4000-8000-000000000999",
                "revoked_at": None,
            },
        ]

        sessions = self.service.list_sessions(principal)
        self.assertEqual([value["current"] for value in sessions], [True, False])
        self.assertFalse(
            self.service.revoke_session(
                principal, "00000000-0000-4000-8000-000000000104"
            )
        )
        self.assertEqual(self.service.logout_all(principal), 1)
        self.assertIsNone(self.store.sessions[2]["revoked_at"])

    def test_password_reset_is_generic_for_unknown_email_and_revokes_sessions(self) -> None:
        registered = self.service.register(
            email="person@example.com",
            password="correct password value",
            display_name="Person",
        )
        user_id = str(registered.user["user_id"])
        self.store.sessions = [
            {"session_id": "session-1", "user_id": user_id, "revoked_at": None}
        ]

        missing = self.service.request_password_reset("missing@example.com")
        delivery = self.service.request_password_reset("person@example.com")

        self.assertIsNone(missing)
        self.assertIsNotNone(delivery)
        assert delivery is not None
        raw_token = delivery.reset_secret.consume()
        self.assertNotIn(raw_token, repr(delivery))
        self.assertEqual(
            self.store.last_action_token["token_hash"],
            hashlib.sha256(raw_token.encode("ascii")).digest(),
        )

        result = self.service.reset_password(raw_token, "replacement password value")

        self.assertEqual(result["status"], "PENDING_VERIFICATION")
        self.assertEqual(
            self.store.users["person@example.com"]["password_hash"],
            "encoded::replacement password value",
        )
        self.assertEqual(self.store.sessions[0]["revoked_at"], "now")
        self.assertEqual(self.store.audits[-1]["category"], "PASSWORD_RESET")

    def test_change_password_requires_current_password_and_revokes_all_sessions(self) -> None:
        registered = self.service.register(
            email="person@example.com",
            password="correct password value",
            display_name="Person",
        )
        principal = {
            "user_id": str(registered.user["user_id"]),
            "email_normalized": "person@example.com",
        }
        self.store.sessions = [
            {"session_id": "session-1", "user_id": principal["user_id"], "revoked_at": None}
        ]

        with self.assertRaisesRegex(AuthRejected, "当前密码错误"):
            self.service.change_password(
                principal,
                current_password="incorrect password value",
                new_password="replacement password value",
            )

        revoked = self.service.change_password(
            principal,
            current_password="correct password value",
            new_password="replacement password value",
        )

        self.assertEqual(revoked, 1)
        self.assertEqual(self.store.sessions[0]["revoked_at"], "now")
        self.assertEqual(self.store.audits[-1]["category"], "PASSWORD_CHANGE")

    def test_resend_verification_rotates_token_without_returning_it_in_repr(self) -> None:
        registered = self.service.register(
            email="person@example.com",
            password="correct password value",
            display_name="Person",
        )
        delivery = self.service.resend_verification(
            {
                "user_id": str(registered.user["user_id"]),
                "status": "PENDING_VERIFICATION",
            }
        )

        raw_token = delivery.verification_secret.consume()
        self.assertNotIn(raw_token, repr(delivery))
        self.assertEqual(
            self.store.last_action_token["token_hash"],
            hashlib.sha256(raw_token.encode("ascii")).digest(),
        )

    def test_authentication_rate_limit_fails_before_password_verification(self) -> None:
        self.store.rate_limit_allowed = False
        with self.assertRaises(AuthRateLimited):
            self.service.login(
                email="person@example.com",
                password="candidate password value",
                ip_address="203.0.113.19",
            )
        self.assertEqual(self.passwords.verify_calls, [])
        self.assertEqual(self.store.rate_limit_calls[0]["bucket"], "LOGIN")


if __name__ == "__main__":
    unittest.main()
