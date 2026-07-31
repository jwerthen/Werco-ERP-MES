"""Per-test SQLite foreign-key enforcement.

SQLite defaults ``PRAGMA foreign_keys`` to **OFF**, and nothing in ``app/db`` or
``tests/`` ever turns it on -- so the entire suite runs with foreign keys UNENFORCED
while production runs on Postgres, where they always are. Any bug of the shape
"delete a parent row that a child still references" is therefore structurally
invisible here. That exact blind spot hid a production-only blocker on the
material-consumption feature's headline flow through a first review.

Lives in its own helper module rather than in one of the test files that uses it:
``tests/`` has no ``__init__.py``, so a test module imported BY another test module is
loaded a second time under a different name (``tests.api.test_x`` alongside pytest's
own ``api.test_x``) and misses pytest's assertion rewriting. A shared helper is
imported once, by everyone, from one path -- the ``kiosk_test_helpers`` precedent.
"""

from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session


@contextmanager
def sqlite_foreign_keys_enforced(db: Session):
    """Turn SQLite's FK enforcement ON for the body of one test, then back OFF.

    Deliberately scoped to a single test rather than enabled suite-wide: flipping it
    globally is a large, independent change (it would newly enforce every FK in ~50
    models against fixture data written in arbitrary order) and belongs in its own PR.

    The pragma is a no-op inside an open transaction, so the session is committed
    first; the assertion below is what proves it actually took effect, and it is
    restored in a ``finally`` so the fixture's ``drop_all`` teardown is unaffected.

    A test using this MUST carry a positive control -- something that FK-violates
    while the pragma is on -- or it cannot tell "the code is correct" apart from
    "enforcement was silently off", which is the suite-wide default.
    """
    db.commit()
    db.execute(text("PRAGMA foreign_keys=ON"))
    assert db.execute(text("PRAGMA foreign_keys")).scalar() == 1, "FK enforcement did not take effect"
    try:
        yield
    finally:
        db.rollback()
        db.execute(text("PRAGMA foreign_keys=OFF"))
        db.commit()
