"""A minimal in-memory stand-in for imaplib.IMAP4_SSL, shared by every
mail test that needs one — the same seam ImapClient's connection_factory
exists for. Not part of _bootstrap.py: that module's contract is the ~6
lines nearly every test repeats (Paths/Database/AppConfig/SecurityService),
and an IMAP fake is a different, mail-specific kind of shared fixture.
"""
from __future__ import annotations

import time

from chatgrab.integrations.mail.imap_client import ImapClient


class FakeImapConnection:
    """Holds the «server» state: a folders dict passed in from outside
    that survives across multiple connections, the way a real mailbox
    survives across multiple syncs."""

    def __init__(self, folders: dict, valid_password: str = "correct-password"):
        self.folders = folders
        self.valid_password = valid_password
        self._selected = None

    def login(self, user, password):
        if password != self.valid_password:
            return "NO", [b"authentication failed"]
        return "OK", [b"logged in"]

    def logout(self):
        return "BYE", [b"bye"]

    def list(self):
        data = [f'(\\HasNoChildren) "/" "{name}"'.encode() for name in self.folders]
        return "OK", data

    def status(self, folder, _what):
        info = self.folders.get(folder)
        if info is None:
            return "NO", [b"no such folder"]
        return "OK", [f'"{folder}" (UIDVALIDITY {info["uidvalidity"]})'.encode()]

    def select(self, folder, readonly=True):
        if folder not in self.folders:
            return "NO", [b"no such folder"]
        self._selected = folder
        return "OK", [str(len(self.folders[folder]["messages"])).encode()]

    def uid(self, command, *args):
        if command == "search":
            return self._search(args)
        if command == "store":
            return self._store(args)
        assert command == "fetch"
        return self._fetch(args)

    def _fetch(self, args):
        seq, _items = args
        msgs = self.folders[self._selected]["messages"]
        if ":" in seq:
            start = int(seq.split(":")[0])
            uids = sorted(u for u in msgs if u >= start)
        elif "," in seq:
            uids = sorted(int(x) for x in seq.split(",") if int(x) in msgs)
        else:
            u = int(seq)
            uids = [u] if u in msgs else []
        data = []
        for u in uids:
            raw = msgs[u]
            data.append((f"{u} (UID {u} BODY[HEADER] {{{len(raw)}}}".encode(), raw))
            data.append(b")")
        return "OK", (data or [None])

    def _search(self, args):
        # ("CHARSET", "UTF-8", "OR", "SUBJECT", b'"term"', "BODY", b'"term"')
        term_bytes = args[4] if len(args) > 4 else b""
        term = term_bytes.decode("utf-8").strip('"').lower().encode("utf-8")
        msgs = self.folders[self._selected]["messages"]
        hits = [str(u) for u, raw in sorted(msgs.items()) if term in raw.lower()]
        return "OK", [" ".join(hits).encode()]

    def _store(self, args):
        seq, _flags_op, _flags = args
        uids = {int(x) for x in seq.split(",")}
        self.folders[self._selected].setdefault("seen", set()).update(uids)
        return "OK", [b"done"]


class SlowFakeImapConnection(FakeImapConnection):
    """Same «server», but every FETCH takes real time — used to prove a
    slow network call doesn't hold Database._lock (see test_mail_sync.py)."""

    def uid(self, *args, **kwargs):
        time.sleep(0.3)
        return super().uid(*args, **kwargs)


def make_client_factory(state_by_host: dict, connection_cls=FakeImapConnection):
    def factory(host, port):
        return ImapClient(host, port, connection_factory=lambda: connection_cls(state_by_host[host]))
    return factory
