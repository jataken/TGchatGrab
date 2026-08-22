"""П5: the one place raw MIME bytes actually leave this app over the
network. Deliberately thin — connection, login, sendmail, quit — the
same shape as ImapClient, with the same connection_factory seam a test
replaces with a fake object exposing login/sendmail/quit, mirroring
_fake_imap.py's approach for IMAP.

Port 465 is implicit TLS (SMTP_SSL, connects already encrypted); 587 is
STARTTLS (plain SMTP, then upgrades). Every provider PLAN.md names
(Yandex, Mail.ru, Gmail, Rambler) defaults to 465 in imap_client.py's
KNOWN_PROVIDERS table, so that's the common path; 587 is there for a
custom/corporate server that only offers STARTTLS.
"""
from __future__ import annotations

import smtplib
from typing import Callable


class SmtpError(Exception):
    pass


class SmtpClient:
    def __init__(self, host: str, port: int = 465,
                 connection_factory: Callable[[], object] | None = None):
        self.host = host
        self.port = port
        self._connection_factory = connection_factory or self._default_connection

    def _default_connection(self):
        if self.port == 587:
            conn = smtplib.SMTP(self.host, self.port, timeout=30)
            conn.starttls()
            return conn
        return smtplib.SMTP_SSL(self.host, self.port, timeout=30)

    def send(self, username: str, password: str, from_address: str,
              to_addresses: list[str], raw_message: bytes) -> None:
        """The only method here that transmits anything — MailService.
        send_draft() (services/mail_service.py) is, in turn, the only
        call site in the whole app that reaches this method; see that
        module's docstring for how П-1 ("никакой автоматической
        отправки") is enforced structurally, not just by convention."""
        try:
            conn = self._connection_factory()
        except OSError as e:
            raise SmtpError(f"не удалось подключиться к {self.host}:{self.port}: {e}") from e
        try:
            conn.login(username, password)
            refused = conn.sendmail(from_address, to_addresses, raw_message)
            if refused:
                raise SmtpError(f"сервер отклонил часть получателей: {refused}")
        except smtplib.SMTPException as e:
            raise SmtpError(f"не удалось отправить письмо: {e}") from e
        finally:
            try:
                conn.quit()
            except Exception:
                pass
