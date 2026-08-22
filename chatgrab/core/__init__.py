"""Domain layer: business rules with no framework attached.

Nothing here may import PySide6, telethon, aiogram, or sqlite3 — see
tests/test_core_no_qt.py, which fails the build if that ever slips. The
point isn't purity for its own sake: it's that the rule "a lead can't
become «отказ» without a reason" should be checkable, and testable, without
starting Qt or touching a database file.

Grows by touch, not upfront: this package holds only what an existing
session actually needed to pull out of `db/database.py` or a UI screen.
See PLAN.md's "Осознанно не делаем" — no four-layer rewrite as its own task.
"""
