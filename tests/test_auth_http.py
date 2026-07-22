from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4


class AuthHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: F401
            from lexsond.web.app import create_app  # noqa: F401
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest("install the web extra") from exc

    def test_required_mode_rejects_anonymous_and_enforces_both_csrf_phases(self) -> None:
        from fastapi.testclient import TestClient

        app, auth_store, _mailer = _required_app()
        with TestClient(app, base_url="https://testserver") as client:
            anonymous = client.get("/api/v1/providers")
            no_login_csrf = client.post(
                "/api/v1/auth/login",
                json={"email": "user@example.com", "password": "correct password"},
            )
            issued = client.get("/api/v1/auth/csrf")
            preauth = issued.json()["data"]["csrf_token"]
            logged_in = client.post(
                "/api/v1/auth/login",
                headers={"X-CSRF-Token": preauth},
                json={"email": "user@example.com", "password": "correct password"},
            )
            raw_session = client.cookies.get("lexsond_session")
            session_csrf = logged_in.json()["data"]["csrf_token"]
            no_session_csrf = client.post("/api/v1/auth/logout")
            logged_out = client.post(
                "/api/v1/auth/logout",
                headers={"X-CSRF-Token": session_csrf},
            )

        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(no_login_csrf.status_code, 403)
        self.assertEqual(issued.status_code, 200)
        self.assertEqual(issued.headers["cache-control"], "no-store")
        self.assertIn("HttpOnly", issued.headers["set-cookie"])
        self.assertNotIn(preauth, issued.headers["set-cookie"])
        self.assertEqual(logged_in.status_code, 200)
        cookie = logged_in.headers["set-cookie"]
        self.assertIn("lexsond_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertIn("Secure", cookie)
        self.assertIsNotNone(raw_session)
        self.assertNotIn(str(raw_session), repr(auth_store.sessions))
        self.assertNotIn(str(raw_session), logged_in.text)
        self.assertEqual(no_session_csrf.status_code, 403)
        self.assertEqual(logged_out.status_code, 200)

    def test_registration_delivers_token_out_of_band_and_never_returns_it(self) -> None:
        from fastapi.testclient import TestClient

        app, _store, mailer = _required_app()
        password = "a correct horse password"
        with TestClient(app, base_url="https://testserver") as client:
            issued = client.get("/api/v1/auth/csrf")
            registered = client.post(
                "/api/v1/auth/register",
                headers={"X-CSRF-Token": issued.json()["data"]["csrf_token"]},
                json={
                    "email": "New.User@Example.com",
                    "password": password,
                    "display_name": "New User",
                },
            )

        self.assertEqual(registered.status_code, 202)
        self.assertEqual(mailer.email, "New.User@Example.com")
        self.assertIsNotNone(mailer.secret)
        self.assertNotIn(str(mailer.secret), registered.text)
        self.assertNotIn(password, registered.text)

    def test_session_response_rotates_csrf_without_exposing_session_cookie(self) -> None:
        from fastapi.testclient import TestClient

        app, store, _mailer = _required_app()
        with TestClient(app, base_url="https://testserver") as client:
            issued = client.get("/api/v1/auth/csrf")
            login = client.post(
                "/api/v1/auth/login",
                headers={"X-CSRF-Token": issued.json()["data"]["csrf_token"]},
                json={"email": "user@example.com", "password": "correct password"},
            )
            raw_session = client.cookies.get("lexsond_session")
            first_csrf = login.json()["data"]["csrf_token"]
            resumed = client.get("/api/v1/auth/session")
            second_csrf = resumed.json()["data"]["csrf_token"]
            providers = client.get("/api/v1/providers")
            old_token_logout = client.post(
                "/api/v1/auth/logout",
                headers={"X-CSRF-Token": first_csrf},
            )

        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.headers["cache-control"], "no-store")
        self.assertEqual(
            set(resumed.json()["data"]["user"]),
            {
                "user_id",
                "email",
                "display_name",
                "avatar_url",
                "status",
                "system_role",
                "email_verified_at",
                "workspace_id",
                "workspace_name",
                "workspace_role",
            },
        )
        self.assertNotEqual(first_csrf, second_csrf)
        self.assertNotIn(str(raw_session), resumed.text)
        self.assertEqual(providers.status_code, 200)
        self.assertEqual(old_token_logout.status_code, 200)

    def test_local_single_user_session_returns_only_public_principal_fields(self) -> None:
        from fastapi.testclient import TestClient
        from lexsond.web.app import create_app
        from lexsond.web.auth import AuthConfiguration

        local_auth_store = _LocalAuthStore()
        service = _Service(auth_store=local_auth_store)
        app = create_app(
            service=service,
            frontend_path="/tmp/lexsond-auth-http-missing",
            auth_configuration=AuthConfiguration.from_values(
                auth_mode="local-single-user",
                listen_host="127.0.0.1",
                cookie_secure=False,
            ),
        )

        with TestClient(app) as client:
            response = client.get("/api/v1/auth/session")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        data = response.json()["data"]
        self.assertEqual(data["auth_mode"], "local-single-user")
        self.assertGreaterEqual(len(data["csrf_token"]), 43)
        self.assertEqual(
            set(data["user"]),
            {
                "user_id",
                "email",
                "display_name",
                "avatar_url",
                "status",
                "system_role",
                "email_verified_at",
                "workspace_id",
                "workspace_name",
                "workspace_role",
            },
        )
        self.assertNotIn("email_normalized", response.text)
        self.assertNotIn("csrf_secret_hash", response.text)

    def test_viewer_role_cannot_mutate_workspace_resources(self) -> None:
        from fastapi.testclient import TestClient

        app, store, _mailer = _required_app()
        store.users["user@example.com"]["workspace_role"] = "VIEWER"
        with TestClient(app, base_url="https://testserver") as client:
            issued = client.get("/api/v1/auth/csrf")
            login = client.post(
                "/api/v1/auth/login",
                headers={"X-CSRF-Token": issued.json()["data"]["csrf_token"]},
                json={"email": "user@example.com", "password": "correct password"},
            )
            for session in store.sessions.values():
                session["workspace_role"] = "VIEWER"
            blocked = client.post(
                "/api/v1/targets",
                headers={"X-CSRF-Token": login.json()["data"]["csrf_token"]},
                json={},
            )

        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.json()["error"]["code"], "WORKSPACE_PERMISSION_DENIED")

    def test_login_rate_limit_returns_429_and_retry_after(self) -> None:
        from fastapi.testclient import TestClient

        app, store, _mailer = _required_app()
        with TestClient(app, base_url="https://testserver") as client:
            issued = client.get("/api/v1/auth/csrf")
            store.rate_limit_allowed = False
            blocked = client.post(
                "/api/v1/auth/login",
                headers={"X-CSRF-Token": issued.json()["data"]["csrf_token"]},
                json={"email": "user@example.com", "password": "candidate password"},
            )

        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.json()["error"]["code"], "AUTH_RATE_LIMITED")
        self.assertEqual(blocked.headers["retry-after"], "900")

    def test_credential_profile_api_requires_idempotency_and_never_returns_key(self) -> None:
        from fastapi.testclient import TestClient

        credential_profiles = _CredentialProfiles()
        app, _store, _mailer = _required_app(
            credential_profiles=credential_profiles
        )
        secret = "sk-api-response-secret-sentinel"
        with TestClient(app, base_url="https://testserver") as client:
            issued = client.get("/api/v1/auth/csrf")
            login = client.post(
                "/api/v1/auth/login",
                headers={"X-CSRF-Token": issued.json()["data"]["csrf_token"]},
                json={"email": "user@example.com", "password": "correct password"},
            )
            csrf = login.json()["data"]["csrf_token"]
            missing_idempotency = client.post(
                "/api/v1/credential-profiles",
                headers={"X-CSRF-Token": csrf},
                json={"label": "Primary", "provider_id": "openai", "api_key": secret},
            )
            created = client.post(
                "/api/v1/credential-profiles",
                headers={
                    "X-CSRF-Token": csrf,
                    "Idempotency-Key": str(uuid4()),
                },
                json={"label": "Primary", "provider_id": "openai", "api_key": secret},
            )

        self.assertEqual(missing_idempotency.status_code, 422)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(credential_profiles.received_secret, secret)
        self.assertNotIn(secret, created.text)
        self.assertNotIn("api_key", created.json()["data"])
        self.assertNotIn("secret_locator", created.json()["data"])

    def test_saved_credential_is_resolved_only_for_catalog_execution(self) -> None:
        from fastapi.testclient import TestClient

        secret = "sk-saved-catalog-execution-sentinel"
        credential_profiles = _CredentialProfiles(execution_secret=secret)
        service = _Service()
        app, _store, _mailer = _required_app(
            credential_profiles=credential_profiles,
            service=service,
        )
        with TestClient(app, base_url="https://testserver") as client:
            issued = client.get("/api/v1/auth/csrf")
            login = client.post(
                "/api/v1/auth/login",
                headers={"X-CSRF-Token": issued.json()["data"]["csrf_token"]},
                json={"email": "user@example.com", "password": "correct password"},
            )
            response = client.post(
                "/api/v1/targets/30000000-0000-4000-8000-000000000001/catalog",
                headers={"X-CSRF-Token": login.json()["data"]["csrf_token"]},
                json={
                    "api_key": None,
                    "credential_profile_id": "40000000-0000-4000-8000-000000000001",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(service.received_catalog_secret, secret)
        self.assertEqual(credential_profiles.execution_provider, "openai")
        self.assertNotIn(secret, response.text)
        self.assertNotIn("api_key", response.text)

    def test_smtp_configuration_never_reprs_password_and_forbids_cleartext_auth(self) -> None:
        from lexsond.web.auth_http import SmtpAuthMailer

        password = "smtp-password-secret-sentinel"
        mailer = SmtpAuthMailer(
            host="smtp.example.com",
            port=465,
            sender="noreply@example.com",
            public_base_url="https://lexsond.example.com",
            username="mailer",
            password=password,
            use_tls=True,
        )
        self.assertNotIn(password, repr(mailer))
        with patch.object(SmtpAuthMailer, "_send") as send:
            mailer.send_password_reset(
                email="user@example.com",
                secret="one-time-reset-secret-value",
            )
        message = send.call_args.args[0].get_content()
        self.assertIn("/reset-password#token=", message)
        self.assertNotIn("/reset-password?token=", message)
        with self.assertRaisesRegex(ValueError, "requires TLS"):
            SmtpAuthMailer(
                host="smtp.example.com",
                port=25,
                sender="noreply@example.com",
                public_base_url="https://lexsond.example.com",
                username="mailer",
                password=password,
                use_tls=False,
            )

    def test_password_reset_response_is_generic_and_token_never_enters_response(self) -> None:
        from fastapi.testclient import TestClient

        app, store, mailer = _required_app()
        new_password = "replacement password value"
        with TestClient(app, base_url="https://testserver") as client:
            csrf = client.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
            unknown = client.post(
                "/api/v1/auth/forgot-password",
                headers={"X-CSRF-Token": csrf},
                json={"email": "missing@example.com"},
            )
            known = client.post(
                "/api/v1/auth/forgot-password",
                headers={"X-CSRF-Token": csrf},
                json={"email": "user@example.com"},
            )
            reset_secret = mailer.reset_secret
            reset = client.post(
                "/api/v1/auth/reset-password",
                headers={"X-CSRF-Token": csrf},
                json={"token": reset_secret, "new_password": new_password},
            )

        self.assertEqual(unknown.status_code, 202)
        self.assertEqual(unknown.json(), known.json())
        self.assertIsNotNone(reset_secret)
        self.assertNotIn(str(reset_secret), known.text)
        self.assertEqual(reset.status_code, 200)
        self.assertNotIn(new_password, reset.text)
        self.assertEqual(
            store.users["user@example.com"]["password_hash"],
            f"hash:{new_password}",
        )


class _PasswordManager:
    def hash(self, password: str) -> str:
        if len(password) < 12:
            raise ValueError("password must contain at least 12 characters")
        return f"hash:{password}"

    def verify(self, password: str, encoded_hash: str) -> bool:
        return encoded_hash == f"hash:{password}"

    def needs_rehash(self, _encoded_hash: str) -> bool:
        return False


class _AuthStore:
    def __init__(self) -> None:
        from lexsond.web.auth import secret_hash

        self.users = {
            "user@example.com": {
                "user_id": "10000000-0000-4000-8000-000000000001",
                "workspace_id": "20000000-0000-4000-8000-000000000001",
                "workspace_name": "Personal",
                "workspace_role": "OWNER",
                "email": "user@example.com",
                "email_normalized": "user@example.com",
                "display_name": "User",
                "avatar_url": None,
                "status": "ACTIVE",
                "system_role": "USER",
                "email_verified_at": datetime.now(UTC).isoformat(),
                "password_hash": "hash:correct password",
            }
        }
        self.sessions: dict[bytes, dict] = {}
        self.verification_hash: bytes | None = None
        self.secret_hash = secret_hash
        self.action_tokens = {}
        self.rate_limit_allowed = True

    def register_user(self, **value):
        user = {
            "user_id": "10000000-0000-4000-8000-000000000002",
            "workspace_id": "20000000-0000-4000-8000-000000000002",
            "workspace_name": "Personal",
            "workspace_role": "OWNER",
            "email": value["email_display"],
            "email_normalized": value["email_normalized"],
            "display_name": value["display_name"],
            "avatar_url": None,
            "status": "PENDING_VERIFICATION",
            "system_role": "USER",
            "email_verified_at": None,
            "password_hash": value["password_hash"],
        }
        self.users[value["email_normalized"]] = user
        self.verification_hash = value["verification_token_hash"]
        return dict(user)

    def find_login_user(self, email_normalized):
        value = self.users.get(email_normalized)
        return dict(value) if value else None

    def update_password_hash(self, user_id, password_hash):
        del user_id, password_hash

    def create_session(self, **value):
        session_id = "30000000-0000-4000-8000-000000000001"
        self.sessions[value["token_hash"]] = {
            **value,
            "csrf_secret_hashes": [value["csrf_secret_hash"]],
            "session_id": session_id,
            "email": "user@example.com",
            "email_normalized": "user@example.com",
            "display_name": "User",
            "avatar_url": None,
            "status": "ACTIVE",
            "system_role": "USER",
            "email_verified_at": datetime.now(UTC).isoformat(),
            "workspace_name": "Personal",
            "workspace_role": "OWNER",
        }
        return {
            "session_id": session_id,
            "user_id": value["user_id"],
            "workspace_id": value["workspace_id"],
        }

    def resolve_session(self, *, token_hash, now):
        del now
        value = self.sessions.get(token_hash)
        return dict(value) if value else None

    def rotate_csrf(self, *, session_id, csrf_secret_hash):
        for value in self.sessions.values():
            if value["session_id"] == session_id:
                value["csrf_secret_hash"] = csrf_secret_hash
                value.setdefault("csrf_secret_hashes", []).append(csrf_secret_hash)
                value["csrf_secret_hashes"] = value["csrf_secret_hashes"][-8:]
                return
        raise AssertionError("session missing")

    def revoke_session_by_token(self, *, token_hash, now):
        del now
        self.sessions.pop(token_hash, None)

    def consume_email_verification(self, **_value):
        raise AssertionError("not used")

    def create_password_reset(self, **value):
        user = self.users.get(value["email_normalized"])
        if user is None:
            return None
        self.action_tokens[value["token_hash"]] = {
            **value,
            "user_id": user["user_id"],
        }
        return {"user_id": user["user_id"], "email": user["email"]}

    def consume_password_reset(self, **value):
        token = self.action_tokens.pop(value["token_hash"], None)
        if token is None:
            from lexsond.web.auth_service import AuthRejected

            raise AuthRejected("重置链接无效或已过期")
        user = next(
            item for item in self.users.values() if item["user_id"] == token["user_id"]
        )
        user["password_hash"] = value["password_hash"]
        self.revoke_all_sessions(user_id=user["user_id"], now=value["now"])
        return {"user_id": user["user_id"], "status": user["status"]}

    def create_email_verification(self, **_value):
        raise AssertionError("not used")

    def change_password(self, **value):
        user = next(
            item for item in self.users.values() if item["user_id"] == value["user_id"]
        )
        user["password_hash"] = value["password_hash"]
        return self.revoke_all_sessions(user_id=value["user_id"], now=value["now"])

    def list_user_sessions(self, *, user_id, limit=50):
        del user_id, limit
        return []

    def revoke_user_session(self, **_value):
        raise AssertionError("not used")

    def revoke_all_sessions(self, *, user_id, now):
        del now
        revoked = 0
        for token_hash, value in list(self.sessions.items()):
            if value["user_id"] == user_id:
                self.sessions.pop(token_hash)
                revoked += 1
        return revoked

    def record_auth_audit(self, **_value):
        return None

    def consume_auth_rate_limit(self, **_value):
        return self.rate_limit_allowed


class _Mailer:
    email: str | None = None
    secret: str | None = None
    reset_secret: str | None = None

    def send_verification(self, *, email: str, secret: str) -> None:
        self.email = email
        self.secret = secret

    def send_password_reset(self, *, email: str, secret: str) -> None:
        self.email = email
        self.reset_secret = secret


class _ControlStore:
    def __init__(self, auth_store=None) -> None:
        self._auth_store = auth_store

    def authentication_store(self):
        return self._auth_store

    def for_workspace(self, _workspace_id):
        return self

    def get_target(self, target_id, include_archived=False):
        del include_archived
        return {
            "id": target_id,
            "workspace_id": "20000000-0000-4000-8000-000000000001",
            "provider_id": "openai",
        }


class _Service:
    def __init__(self, auth_store=None) -> None:
        self.store = _ControlStore(auth_store)
        self.received_catalog_secret: str | None = None

    @contextmanager
    def operation(self):
        yield

    def close(self):
        return None

    def target_catalog(self, target_id, api_key, **kwargs):
        self.received_catalog_secret = api_key
        return {
            "status": "CONNECTED",
            "target_id": target_id,
            "auth_mode": "bearer",
            "model_count": 1,
            "models": [{"id": "gpt-test", "probe_types": ["chat"]}],
            "catalog_snapshot_id": "50000000-0000-4000-8000-000000000001",
            "catalog_expires_at": "2026-07-22T01:00:00Z",
            **{key: value for key, value in kwargs.items() if key == "credential_profile_id"},
        }


class _LocalAuthStore:
    def ensure_local_principal(self):
        return {
            "session_id": None,
            "user_id": "10000000-0000-4000-8000-000000000010",
            "workspace_id": "20000000-0000-4000-8000-000000000010",
            "workspace_name": "本地个人工作区",
            "workspace_role": "OWNER",
            "email": "local@lexsond.invalid",
            "email_normalized": "local@lexsond.invalid",
            "display_name": "本地用户",
            "avatar_url": None,
            "status": "ACTIVE",
            "system_role": "USER",
            "email_verified_at": "2026-07-22T00:00:00+00:00",
            "auth_mode": "local-single-user",
            "csrf_secret_hash": b"must-not-be-returned",
        }


class _CredentialProfiles:
    received_secret: str | None = None

    def __init__(self, execution_secret: str | None = None) -> None:
        self.execution_secret = execution_secret
        self.execution_provider: str | None = None

    def status(self):
        return {"available": True, "persistence_enabled": True}

    def create(self, **value):
        self.received_secret = value.pop("api_key").get_secret_value()
        value.pop("actor_user_id")
        value.pop("idempotency_key")
        return {
            "id": "40000000-0000-4000-8000-000000000001",
            "workspace_id": value["workspace_id"],
            "label": value["label"],
            "provider_id": value["provider_id"],
            "storage_backend": "SYSTEM_KEYRING",
            "masked_suffix": "inel",
            "status": "ACTIVE",
            "version": 1,
        }

    def get_for_execution(self, _credential_id, *, workspace_id, provider_id):
        from pydantic import SecretStr

        del workspace_id
        self.execution_provider = provider_id
        if self.execution_secret is None:
            raise AssertionError("execution secret was not configured")
        return SecretStr(self.execution_secret)

    def get(self, credential_id, *, workspace_id, include_archived=False):
        del credential_id, workspace_id, include_archived
        return {"version": 1}

    def fingerprint_for_execution(self, secret):
        self.received_secret = secret.get_secret_value()
        return "f" * 64


def _required_app(*, credential_profiles=None, service=None):
    from lexsond.credentials import ExecutionCredentialBinder
    from lexsond.web.app import create_app
    from lexsond.web.auth import AuthConfiguration
    from lexsond.web.auth_service import AuthenticationService

    auth_store = _AuthStore()
    password_manager = _PasswordManager()
    authentication = AuthenticationService(
        store=auth_store,
        password_manager=password_manager,
        dummy_password_hash=password_manager.hash("dummy password value"),
    )
    mailer = _Mailer()
    app = create_app(
        service=service or _Service(),
        authentication=authentication,
        auth_mailer=mailer,
        credential_profiles=credential_profiles,
        credential_binder=ExecutionCredentialBinder(b"t" * 32),
        frontend_path="/tmp/lexsond-auth-http-missing",
        auth_configuration=AuthConfiguration.from_values(
            auth_mode="required",
            listen_host="127.0.0.1",
            cookie_secure=True,
        ),
    )
    return app, auth_store, mailer


if __name__ == "__main__":
    unittest.main()
