"""Р1/Р2: database.py's 165 methods split by domain, one mixin per file,
composed back into a single `Database` class in db/database.py.

Every mixin assumes it's being mixed into Database — it uses `self._conn`,
`self._lock`, `self.query`/`self.query_one`/`self.execute`/`self.executemany`
(the low-level layer, which stays in database.py itself, not a mixin) and
occasionally another mixin's own public method. None of that is enforced by
a common base class or Protocol here: the project has no type-checking gate
in CI to make one earn its keep, and every mixin only ever ends up on the
one class that already provides all of it.

Purely a file-organization move — see PLAN.md's Р1/Р2 journal entries.
Nothing in here changes a method's SQL, its signature, or its behavior, and
no caller anywhere in the app (UI, bots/, services/, tests) needed to
change: `db.method(...)` still means exactly what it meant before.
"""
