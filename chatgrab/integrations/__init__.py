"""Outbound integrations with systems outside Telegram — Bitrix24 CRM for
now (С6/С7). Each integration owns its own credential storage/rotation
and low-level client; the queue/drain machinery that uses them lives in
services/, matching how bots/outbox.py is the account-safety layer while
the runners own the actual Telegram transport."""
