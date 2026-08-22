"""A minimal in-memory stand-in for imaplib.IMAP4_SSL, shared by every
mail test that needs one — the same seam ImapClient's connection_factory
exists for. Not part of _bootstrap.py: that module's contract is the ~6
lines nearly every test repeats (Paths/Database/AppConfig/SecurityService),
and an IMAP fake is a different, mail-specific kind of shared fixture.

П4 widened this considerably: folder admin (create/rename/delete/
subscribe), move/copy/append/expunge, and per-UID flags beyond \\Seen —
each folder dict now optionally carries "special_use" and every message
its own entry in "flags" (a dict of uid -> set of flag strings), which
_fetch()/_store() both read and write so a round trip through ImapClient
sees exactly what a real server would report back.
"""
from __future__ import annotations

import time

from chatgrab.integrations.mail.imap_client import ImapClient


class FakeImapConnection:
    """Holds the «server» state: a folders dict passed in from outside
    that survives across multiple connections, the way a real mailbox
    survives across multiple syncs. Each folder: {"uidvalidity": int,
    "messages": {uid: raw_bytes}, "special_use": str | None (optional),
    "flags": {uid: {"\\Seen", ...}} (created lazily)}."""

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
        data = []
        for name, info in self.folders.items():
            attrs = ["\\HasNoChildren"]
            special = info.get("special_use")
            if special:
                attrs.append(f"\\{special}")
            if info.get("noselect"):
                attrs.append("\\Noselect")
            data.append(f'({" ".join(attrs)}) "/" "{name}"'.encode())
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

    def capability(self):
        return "OK", [b"IMAP4rev1 IDLE MOVE UIDPLUS"]

    # ---- folder admin (П4) ---------------------------------------------
    def create(self, name):
        if name in self.folders:
            return "NO", [b"already exists"]
        self.folders[name] = {"uidvalidity": 1, "messages": {}}
        return "OK", [b"created"]

    def rename(self, old, new):
        if old not in self.folders:
            return "NO", [b"no such folder"]
        self.folders[new] = self.folders.pop(old)
        return "OK", [b"renamed"]

    def delete(self, name):
        if name not in self.folders:
            return "NO", [b"no such folder"]
        del self.folders[name]
        return "OK", [b"deleted"]

    def subscribe(self, name):
        if name not in self.folders:
            return "NO", [b"no such folder"]
        self.folders[name]["subscribed"] = True
        return "OK", [b"subscribed"]

    def unsubscribe(self, name):
        if name not in self.folders:
            return "NO", [b"no such folder"]
        self.folders[name]["subscribed"] = False
        return "OK", [b"unsubscribed"]

    @staticmethod
    def _allocate_uid(info: dict) -> int:
        """Real IMAP UIDs are strictly increasing and never reused, even
        once every message in a folder has been deleted (RFC 3501) — so
        this tracks its own counter rather than deriving "next" from
        whatever's currently present, which would silently reuse an id
        the moment a folder emptied out and mask a real UID-collision
        bug the same shape as the one this fake's earlier version did
        mask (see П5's journal, PLAN.md)."""
        current = info.get("_uid_counter", max(info["messages"], default=0))
        next_uid = current + 1
        info["_uid_counter"] = next_uid
        return next_uid

    def append(self, folder, flags, date_time, message):
        if folder not in self.folders:
            return "NO", [b"no such folder"]
        info = self.folders[folder]
        next_uid = self._allocate_uid(info)
        info["messages"][next_uid] = message
        if flags:
            names = flags.strip("()").split()
            info.setdefault("flags", {})[next_uid] = set(names)
        return "OK", [f"[APPENDUID {info['uidvalidity']} {next_uid}]".encode()]

    def expunge(self):
        info = self.folders[self._selected]
        deleted = [u for u, f in info.get("flags", {}).items() if "\\Deleted" in f]
        for u in deleted:
            info["messages"].pop(u, None)
            info.get("flags", {}).pop(u, None)
            info.get("seen", set()).discard(u)
        return "OK", [str(len(deleted)).encode()]

    def uid(self, command, *args):
        if command == "search":
            return self._search(args)
        if command == "store":
            return self._store(args)
        if command == "fetch":
            return self._fetch(args)
        if command == "move":
            return self._move(args)
        if command == "copy":
            return self._copy(args)
        if command == "expunge":
            return self._uid_expunge(args)
        raise AssertionError(f"unexpected UID command: {command}")

    def _fetch(self, args):
        seq, _items = args
        info = self.folders[self._selected]
        msgs = info["messages"]
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
            flags = " ".join(sorted(info.get("flags", {}).get(u, ())))
            meta = f"{u} (UID {u} FLAGS ({flags}) BODY[HEADER] {{{len(raw)}}}".encode()
            data.append((meta, raw))
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
        seq, flags_op, flags = args
        uids = {int(x) for x in seq.split(",")}
        names = flags.strip("()").split()
        info = self.folders[self._selected]
        flag_map = info.setdefault("flags", {})
        for u in uids:
            current = flag_map.setdefault(u, set())
            if flags_op == "+FLAGS":
                current.update(names)
            else:
                current.difference_update(names)
        if "\\Seen" in names:
            seen = info.setdefault("seen", set())
            if flags_op == "+FLAGS":
                seen.update(uids)
            else:
                seen.difference_update(uids)
        return "OK", [b"done"]

    def _move(self, args):
        uid_s, dest = args
        info = self.folders[self._selected]
        dest_info = self.folders.get(dest)
        if dest_info is None:
            return "NO", [b"no such destination folder"]
        u = int(uid_s)
        if u not in info["messages"]:
            return "NO", [b"no such message"]
        raw = info["messages"].pop(u)
        flags = info.get("flags", {}).pop(u, set())
        next_uid = self._allocate_uid(dest_info)
        dest_info["messages"][next_uid] = raw
        if flags:
            dest_info.setdefault("flags", {})[next_uid] = set(flags)
        info.get("seen", set()).discard(u)
        return "OK", [b"moved"]

    def _copy(self, args):
        uid_s, dest = args
        info = self.folders[self._selected]
        dest_info = self.folders.get(dest)
        if dest_info is None:
            return "NO", [b"no such destination folder"]
        u = int(uid_s)
        if u not in info["messages"]:
            return "NO", [b"no such message"]
        raw = info["messages"][u]
        next_uid = self._allocate_uid(dest_info)
        dest_info["messages"][next_uid] = raw
        return "OK", [b"copied"]

    def _uid_expunge(self, args):
        """UID EXPUNGE — only the named UID(s), unlike plain expunge()."""
        (seq,) = args
        target = {int(x) for x in seq.split(",")}
        info = self.folders[self._selected]
        for u in target & set(info["messages"]):
            if "\\Deleted" in info.get("flags", {}).get(u, ()):
                info["messages"].pop(u, None)
                info.get("flags", {}).pop(u, None)
                info.get("seen", set()).discard(u)
        return "OK", [b"expunged"]


class NoUidExpungeConnection(FakeImapConnection):
    """A server without the UIDPLUS extension — UID EXPUNGE must be
    rejected so ImapClient's fallback to plain EXPUNGE gets exercised."""

    def _uid_expunge(self, args):
        return "NO", [b"UID EXPUNGE not supported"]


class NoMoveConnection(FakeImapConnection):
    """A server without RFC 6851 MOVE — forces ImapClient's COPY +
    \\Deleted + EXPUNGE fallback path."""

    def _move(self, args):
        return "NO", [b"MOVE not supported"]


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
