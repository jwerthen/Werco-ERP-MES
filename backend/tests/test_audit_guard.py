"""``app.api.audit_guard.required_audit`` -- the shared fail-closed seam for REQUIRED audit rows.

Built in the floor-gates PR (POST /work-orders/{id}/resume) and reused by the stacked
short-count PR at five more sites, so its contract is pinned here directly rather than only
through one route:

* ``AuditWriteError`` inside the block -> ``db.rollback()`` exactly once, then HTTP 503 with
  the caller's detail verbatim (chained from the audit error);
* any OTHER exception passes through untouched and does NOT roll back -- the caller's own
  StaleDataError / IntegrityError / HTTPException handling still owns those;
* a clean block neither rolls back nor raises.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.orm.exc import StaleDataError

from app.api.audit_guard import REQUIRED_AUDIT_UNAVAILABLE_STATUS, required_audit
from app.services.audit_service import AuditWriteError

pytestmark = pytest.mark.unit

DETAIL = "The audit record for X could not be saved; nothing was changed. Retry."


def test_audit_write_error_rolls_back_once_and_raises_503_with_the_detail_verbatim():
    db = MagicMock()
    with pytest.raises(HTTPException) as caught:
        with required_audit(db, DETAIL):
            raise AuditWriteError("The required audit record could not be saved")

    assert caught.value.status_code == REQUIRED_AUDIT_UNAVAILABLE_STATUS == 503
    assert caught.value.detail == DETAIL
    assert isinstance(caught.value.__cause__, AuditWriteError)
    db.rollback.assert_called_once_with()
    db.commit.assert_not_called()


@pytest.mark.parametrize(
    "exc",
    [
        HTTPException(status_code=409, detail="This work order was modified concurrently. Refresh and retry."),
        StaleDataError("version mismatch"),
        ValueError("not an audit failure"),
    ],
    ids=["http_409", "stale_data", "value_error"],
)
def test_other_exceptions_pass_through_untouched_and_do_not_roll_back(exc):
    db = MagicMock()
    with pytest.raises(type(exc)) as caught:
        with required_audit(db, DETAIL):
            raise exc

    assert caught.value is exc
    db.rollback.assert_not_called()


def test_a_clean_block_neither_rolls_back_nor_raises():
    db = MagicMock()
    with required_audit(db, DETAIL):
        pass
    db.rollback.assert_not_called()
