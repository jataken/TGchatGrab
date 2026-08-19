"""Mail protocol clients (IMAP now, SMTP in П5) — talks to one mailbox's
server, knows nothing about the database or the sync loop. See
services/mail_service.py for the part that decides *when* to call this
and what to do with what comes back.
"""
