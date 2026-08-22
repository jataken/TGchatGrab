"""П5: a minimal in-memory stand-in for smtplib's SMTP/SMTP_SSL, the SMTP
counterpart to _fake_imap.py's FakeImapConnection — same login/perform/
close shape, same connection_factory seam SmtpClient exposes for it.
"""
from __future__ import annotations

import smtplib

from chatgrab.integrations.mail.smtp_client import SmtpClient


class FakeSmtpConnection:
    """sent_log is a list passed in from outside (like FakeImapConnection's
    folders dict) so a test can inspect what actually got "sent" after
    the call returns."""

    def __init__(self, sent_log: list, valid_password: str = "correct-password", refused: dict | None = None):
        self.sent_log = sent_log
        self.valid_password = valid_password
        self.refused = refused or {}

    def login(self, user, password):
        if password != self.valid_password:
            raise smtplib.SMTPAuthenticationError(535, b"authentication failed")

    def sendmail(self, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):
        self.sent_log.append({"from": from_addr, "to": list(to_addrs), "raw": msg})
        return dict(self.refused)

    def quit(self):
        pass


def make_smtp_factory(sent_log: list, connection_cls=FakeSmtpConnection, valid_password="correct-password"):
    def factory(host, port):
        return SmtpClient(host, port, connection_factory=lambda: connection_cls(sent_log, valid_password))
    return factory
