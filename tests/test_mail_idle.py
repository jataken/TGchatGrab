"""П4: ImapClient.idle() at the raw-socket level it actually operates at
— imaplib has no public IDLE support (see imap_client.py's module
docstring for the private-API workaround this uses), so this test
doesn't go through the higher-level FakeImapConnection at all: IDLE
reads untagged lines directly off a "socket", which needs its own,
differently-shaped fake — a scripted queue of lines to hand back from
readline(), not a dispatch-by-command object like the rest of this
app's IMAP tests use.
"""
import socket
import sys
import threading
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatgrab.integrations.mail.imap_client import ImapClient, ImapError


class _FakeSock:
    def settimeout(self, seconds):
        pass  # the fake never actually blocks, so a real timeout has nothing to enforce


class _FakeIdleConn:
    """`lines` is consumed front-to-back by readline(); once empty, every
    further readline() raises socket.timeout — the same signal a real
    blocking socket gives on a read that timed out with nothing to
    report, which is exactly what idle()'s inner loop is built to
    tolerate and keep looping past."""

    def __init__(self, lines):
        self.sock = _FakeSock()
        self.sent: list[bytes] = []
        self._lines = list(lines)
        self._tag_n = 0

    def select(self, folder, readonly=True):
        return "OK", [b"1"]

    def _new_tag(self):
        self._tag_n += 1
        return f"A{self._tag_n}".encode()

    def send(self, data):
        self.sent.append(data)

    def readline(self):
        if not self._lines:
            raise socket.timeout()
        return self._lines.pop(0)


print("== IDLE: untagged EXISTS fires on_event, DONE is sent on stop ==")
conn = _FakeIdleConn([
    b"+ idling\r\n",
    b"* 5 EXISTS\r\n",
    b"A1 OK IDLE terminated\r\n",
])
client = ImapClient("host", 993, connection_factory=lambda: conn)
client._conn = conn  # idle() only ever touches self._conn — connect()/login() aren't part of this test
stop_event = threading.Event()
events = []


def _on_event():
    events.append(True)
    stop_event.set()


client.idle("INBOX", _on_event, stop_event)
print("  событий получено:", len(events))
assert events == [True]
print("  отправлено:", conn.sent)
assert conn.sent[0] == b"A1 IDLE\r\n"
assert conn.sent[-1] == b"DONE\r\n"
print("  ok — DONE отправлен сразу после сигнала остановки")


print("\n== IDLE: сервер отклонил IDLE — понятная ошибка, не тихий зависон ==")
conn2 = _FakeIdleConn([b"NO IDLE not supported\r\n"])
client2 = ImapClient("host", 993, connection_factory=lambda: conn2)
client2._conn = conn2
try:
    client2.idle("INBOX", lambda: None, threading.Event())
    raise AssertionError("должен был поднять ImapError")
except ImapError as e:
    print("  ok:", e)


print("\n== IDLE: переиздаётся до истечения интервала обновления, не раз в сессию ==")
conn3 = _FakeIdleConn([b"+ idling\r\n", b"+ idling\r\n"])
client3 = ImapClient("host", 993, connection_factory=lambda: conn3)
client3._conn = conn3
original_refresh = ImapClient._IDLE_REFRESH_SECONDS
ImapClient._IDLE_REFRESH_SECONDS = 0.05
stop_event3 = threading.Event()


def _run():
    try:
        client3.idle("INBOX", lambda: None, stop_event3)
    except Exception:
        pass  # ожидаемо, когда у фейка кончаются заготовленные строки


thread = threading.Thread(target=_run, daemon=True)
thread.start()
deadline = time.monotonic() + 3.0
while time.monotonic() < deadline:
    if len([s for s in conn3.sent if s.endswith(b" IDLE\r\n")]) >= 2:
        break
    time.sleep(0.01)
stop_event3.set()
thread.join(timeout=2)
ImapClient._IDLE_REFRESH_SECONDS = original_refresh

idle_sends = [s for s in conn3.sent if s.endswith(b" IDLE\r\n")]
print("  IDLE отправлен раз:", len(idle_sends), conn3.sent)
assert len(idle_sends) >= 2, "IDLE должен переиздаваться до истечения _IDLE_REFRESH_SECONDS, не только один раз"
print("  ok")

print("\nТЕСТ ПРОЙДЕН: IDLE читает untagged-события, завершается по сигналу, переиздаётся вовремя")
