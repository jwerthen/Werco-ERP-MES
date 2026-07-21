"""The kiosk-scope path fence in ``get_current_user``.

A badge-minted operator token carries ``scope="kiosk"`` (see
``POST /auth/kiosk-badge-token``). ``get_current_user`` honors it ONLY on paths
under ``/api/v1/shop-floor`` (+ ``/api/v1/auth/employee-logout``) and returns
**403** everywhere else — the token is valid, it just cannot reach the
resource. Tokens WITHOUT a scope claim are completely unaffected.

The fence is what makes the 5-minute kiosk operator token safe: even inside its
lifetime it can never read quotes, users, or work-order admin surfaces, and it
can never be exchanged for a refresh token.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.user import UserRole
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    bearer,
    make_user,
    make_wo_with_operation,
    make_work_center,
    queue_url,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

FENCE_DETAIL = "Kiosk-scoped token cannot access this resource"

# Endpoints that must stay REACHABLE for a kiosk-scoped operator token.
# (The shop-floor mutations share the same dependency; my-active-job and the
# queue are the cheap read representatives.)
ALLOWED_GET_PATHS = ["/api/v1/shop-floor/my-active-job"]

# Representative fenced surfaces: user admin, quoting, work-order admin.
FENCED_GET_PATHS = ["/api/v1/users/", "/api/v1/quotes/", "/api/v1/work-orders/"]


def _kiosk_scoped_token(user, company_id: int = COMPANY_A) -> str:
    return create_access_token(subject=user.id, company_id=company_id, scope="kiosk")


def test_kiosk_scoped_token_allowed_on_shop_floor(client: TestClient, db_session: Session):
    """scope='kiosk' is honored on shop-floor paths (200, acting as the user)."""
    operator = make_user(db_session, company_id=COMPANY_A, role=UserRole.OPERATOR)
    token = _kiosk_scoped_token(operator)

    for path in ALLOWED_GET_PATHS:
        resp = client.get(path, headers=bearer(token))
        assert resp.status_code == status.HTTP_200_OK, f"{path}: {resp.status_code} {resp.text}"

    wc = make_work_center(db_session, company_id=COMPANY_A)
    queue = client.get(queue_url(wc.id), headers=bearer(token))
    assert queue.status_code == status.HTTP_200_OK, queue.text
    # A user-path caller gets a null station block (not a station principal).
    assert queue.json()["station"] is None


@pytest.mark.parametrize("path", FENCED_GET_PATHS)
def test_kiosk_scoped_token_403_outside_shop_floor(client: TestClient, db_session: Session, path):
    """scope='kiosk' is 403 outside the fence — with the fence's own detail
    string, so this is the scope check firing, not RBAC."""
    # Even an ADMIN's kiosk-scoped token is fenced: the scope claim governs,
    # not the role — proving the 403 cannot be an RBAC coincidence.
    admin = make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    token = _kiosk_scoped_token(admin)

    resp = client.get(path, headers=bearer(token))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, f"{path}: {resp.status_code} {resp.text}"
    assert resp.json()["detail"] == FENCE_DETAIL


def test_kiosk_scoped_token_cannot_refresh(client: TestClient, db_session: Session):
    """The kiosk operator token can never be laundered into a session: posted
    as a refresh_token it fails verification (401) — no refresh, no extension."""
    operator = make_user(db_session, company_id=COMPANY_A)
    token = _kiosk_scoped_token(operator)

    resp = client.post(
        "/api/v1/auth/refresh",
        headers={"X-Requested-With": "XMLHttpRequest"},
        json={"refresh_token": token},
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text


def test_kiosk_scoped_token_allowed_on_employee_logout(client: TestClient, db_session: Session):
    """/auth/employee-logout stays reachable from the kiosk (the logout audit
    write is part of the kiosk flow)."""
    operator = make_user(db_session, company_id=COMPANY_A)
    token = _kiosk_scoped_token(operator)

    resp = client.post(
        "/api/v1/auth/employee-logout",
        headers=bearer(token),
        json={"employee_id": operator.employee_id},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text


@pytest.mark.parametrize("path", FENCED_GET_PATHS + ALLOWED_GET_PATHS)
def test_unscoped_tokens_unaffected(client: TestClient, db_session: Session, path):
    """A normal (scope-less) access token behaves exactly as before on every
    one of these paths — anything but the fence's 403."""
    admin = make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    token = create_access_token(subject=admin.id, company_id=COMPANY_A)

    resp = client.get(path, headers=bearer(token))
    assert resp.status_code == status.HTTP_200_OK, f"{path}: {resp.status_code} {resp.text}"


def test_scope_claim_round_trip(client: TestClient, db_session: Session):
    """Sanity: verify_token surfaces scope for scoped tokens and None otherwise."""
    from app.core.security import verify_token

    operator = make_user(db_session, company_id=COMPANY_A)
    scoped = verify_token(_kiosk_scoped_token(operator))
    assert scoped is not None and scoped["scope"] == "kiosk"

    unscoped = verify_token(create_access_token(subject=operator.id, company_id=COMPANY_A))
    assert unscoped is not None and unscoped["scope"] is None


# --- Deny-list carve-outs inside the shop-floor prefix ----------------------
# The crew station never needs these, so even a MANAGER/ADMIN's 5-minute
# badge-minted token must not reach them: station lifecycle admin would let a
# scanned manager badge persist access (PIN reset), and labor approval (G5-A)
# is a desktop supervisor workflow.

STATION_ADMIN_DENIED = [
    ("get", "/api/v1/shop-floor/kiosk-stations"),
    ("post", "/api/v1/shop-floor/kiosk-stations"),
    ("post", "/api/v1/shop-floor/kiosk-stations/999999/revoke"),
    ("post", "/api/v1/shop-floor/kiosk-stations/999999/reset-pin"),
]

APPROVAL_DENIED = [
    ("post", "/api/v1/shop-floor/time-entries/999999/approve"),
    ("post", "/api/v1/shop-floor/time-entries/999999/unapprove"),
]


@pytest.mark.parametrize("method,path", STATION_ADMIN_DENIED + APPROVAL_DENIED)
def test_kiosk_scoped_token_403_on_denied_shop_floor_paths(client: TestClient, db_session: Session, method, path):
    """Even an ADMIN's kiosk-scoped token is fenced off the station-admin and
    labor-approval endpoints — the fence's own detail proves it fires before
    RBAC or any 404 existence check."""
    admin = make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    token = _kiosk_scoped_token(admin)

    if method == "post":
        resp = client.post(path, headers=bearer(token), json={})
    else:
        resp = client.get(path, headers=bearer(token))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, f"{method} {path}: {resp.status_code} {resp.text}"
    assert resp.json()["detail"] == FENCE_DETAIL


def test_kiosk_scoped_token_403_on_the_manager_dispatch_tools(client: TestClient, db_session: Session):
    """The dispatch board and the run-order rewrite live UNDER ``/shop-floor``,
    so the prefix fence alone would have let a shared crew station read the whole
    shop's board and dictate what every machine runs next. Both are denied, on a
    real work-center id so this cannot pass by accident on a 404."""
    admin = make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    token = _kiosk_scoped_token(admin)
    wc = make_work_center(db_session, company_id=COMPANY_A)

    board = client.get("/api/v1/shop-floor/dispatch-board", headers=bearer(token))
    assert board.status_code == status.HTTP_403_FORBIDDEN, board.text
    assert board.json()["detail"] == FENCE_DETAIL

    rewrite = client.put(
        f"/api/v1/shop-floor/work-centers/{wc.id}/run-order",
        headers=bearer(token),
        json={"operation_ids": []},
    )
    assert rewrite.status_code == status.HTTP_403_FORBIDDEN, rewrite.text
    assert rewrite.json()["detail"] == FENCE_DETAIL


def test_operators_keep_reading_the_run_chips(client: TestClient, db_session: Session):
    """The deny-list must not cost the shop its RUN chips: the queue read is a
    different path and stays reachable, both for a normal operator token and for
    a badge-minted kiosk-scoped one."""
    manager = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    operator = make_user(db_session, company_id=COMPANY_A, role=UserRole.OPERATOR)
    wc = make_work_center(db_session, company_id=COMPANY_A)
    _, op = make_wo_with_operation(db_session, company_id=COMPANY_A, work_center=wc)

    ranked = client.put(
        f"/api/v1/shop-floor/work-centers/{wc.id}/run-order",
        headers=user_headers(manager),
        json={"operation_ids": [op.id]},
    )
    assert ranked.status_code == status.HTTP_200_OK, ranked.text

    for headers in (user_headers(operator), bearer(_kiosk_scoped_token(operator))):
        queue = client.get(queue_url(wc.id), headers=headers)
        assert queue.status_code == status.HTTP_200_OK, queue.text
        assert [row["run_order"] for row in queue.json()["queue"]] == [1]


def test_unscoped_admin_still_reaches_station_admin(client: TestClient, db_session: Session):
    """The deny-list is scope-keyed, not path-dead: a normal (scope-less) ADMIN
    token lists stations fine."""
    admin = make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    token = create_access_token(subject=admin.id, company_id=COMPANY_A)

    resp = client.get("/api/v1/shop-floor/kiosk-stations", headers=bearer(token))
    assert resp.status_code == status.HTTP_200_OK, resp.text
