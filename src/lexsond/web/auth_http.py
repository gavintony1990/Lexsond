from __future__ import annotations

import hmac
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Protocol
from urllib.parse import quote

from .auth import OneTimeSecret, issue_one_time_secret, secret_matches
from .auth_service import CsrfRejected


PREAUTH_CSRF_COOKIE = "lexsond_preauth_csrf"


class AuthMailer(Protocol):
    def send_verification(self, *, email: str, secret: str) -> None: ...

    def send_password_reset(self, *, email: str, secret: str) -> None: ...


class AuthDeliveryUnavailable(RuntimeError):
    pass


class PreAuthCsrf:
    """Stateless pre-session CSRF using an HttpOnly hash cookie."""

    def issue(self) -> tuple[OneTimeSecret, str]:
        secret, digest = issue_one_time_secret()
        return secret, digest.hex()

    def verify(self, *, raw_header: str | None, cookie_hash: str | None) -> None:
        try:
            expected = bytes.fromhex(cookie_hash or "")
        except ValueError as exc:
            raise CsrfRejected("CSRF 校验失败") from exc
        if len(expected) != 32 or not raw_header or not secret_matches(
            raw_header, expected
        ):
            raise CsrfRejected("CSRF 校验失败")


@dataclass(frozen=True, slots=True)
class SmtpAuthMailer:
    host: str
    port: int
    sender: str
    public_base_url: str
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    use_tls: bool = True

    def __post_init__(self) -> None:
        if not self.host or any(ord(character) < 0x21 for character in self.host):
            raise ValueError("SMTP host is invalid")
        if not 1 <= self.port <= 65535:
            raise ValueError("SMTP port is out of range")
        if not self.sender or any(character in self.sender for character in "\r\n"):
            raise ValueError("SMTP sender is invalid")
        if not self.public_base_url.startswith("https://"):
            raise ValueError("public base URL must use HTTPS")
        if bool(self.username) != bool(self.password):
            raise ValueError("SMTP username and password must be configured together")
        if self.username and not self.use_tls:
            raise ValueError("authenticated SMTP requires TLS")

    @classmethod
    def from_environment(cls) -> SmtpAuthMailer | None:
        host = os.environ.get("LEXSOND_SMTP_HOST")
        sender = os.environ.get("LEXSOND_SMTP_FROM")
        public_base_url = os.environ.get("LEXSOND_PUBLIC_BASE_URL")
        if not host or not sender or not public_base_url:
            return None
        if not public_base_url.startswith("https://"):
            raise ValueError("LEXSOND_PUBLIC_BASE_URL must use HTTPS")
        try:
            port = int(os.environ.get("LEXSOND_SMTP_PORT", "465"))
        except ValueError as exc:
            raise ValueError("LEXSOND_SMTP_PORT must be an integer") from exc
        return cls(
            host=host,
            port=port,
            sender=sender,
            public_base_url=public_base_url.rstrip("/"),
            username=os.environ.get("LEXSOND_SMTP_USERNAME"),
            password=os.environ.get("LEXSOND_SMTP_PASSWORD"),
            use_tls=os.environ.get("LEXSOND_SMTP_TLS", "true").lower()
            not in {"0", "false", "no", "off"},
        )

    def send_verification(self, *, email: str, secret: str) -> None:
        link = f"{self.public_base_url}/verify-email#token={quote(secret, safe='')}"
        message = EmailMessage()
        message["Subject"] = "验证你的 Lexsond 邮箱"
        message["From"] = self.sender
        message["To"] = email
        message.set_content(
            "请在 24 小时内打开下面的链接完成邮箱验证：\n\n"
            f"{link}\n\n如果不是你发起的注册，请忽略此邮件。"
        )
        self._send(message, failure_message="验证邮件暂时无法发送")

    def send_password_reset(self, *, email: str, secret: str) -> None:
        link = f"{self.public_base_url}/reset-password#token={quote(secret, safe='')}"
        message = EmailMessage()
        message["Subject"] = "重置你的 Lexsond 密码"
        message["From"] = self.sender
        message["To"] = email
        message.set_content(
            "请在 1 小时内打开下面的链接重置密码：\n\n"
            f"{link}\n\n如果不是你发起的请求，请忽略此邮件。"
        )
        self._send(message, failure_message="密码重置邮件暂时无法发送")

    def _send(self, message: EmailMessage, *, failure_message: str) -> None:
        try:
            if self.use_tls:
                client: smtplib.SMTP = smtplib.SMTP_SSL(
                    self.host,
                    self.port,
                    timeout=10,
                    context=ssl.create_default_context(),
                )
            else:
                client = smtplib.SMTP(self.host, self.port, timeout=10)
            with client:
                if self.username:
                    client.login(self.username, self.password or "")
                client.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise AuthDeliveryUnavailable(failure_message) from exc
