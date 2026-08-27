"""``GET /work-orders/?deleted_only=true`` — the work-order restore view.

A soft-deleted work order used to be invisible to every API caller: the list filters
``is_deleted == False`` and the detail read 404s, so ``POST /work-orders/{id}/restore``
existed with nothing able to find what to hand it. ``deleted_only`` is the archive that
makes the restore verb reachable.

Sections, and the failure each one exists to catch:

* **§1 Partition.** The two views split ONE fixture set, asserted in BOTH directions by
  work-order number. A one-directional assertion passes against a flag that returns
  everything.

* **§2 Tenancy (invariant 1).** Company B's soft-deleted work order must never appear
  in company A's archive. ``deleted_only`` inverts the ``is_deleted`` predicate and
  NOTHING else — it can never widen the tenant scope. Asserted on the specific
  work-order number, never on a row count, because a count passes when the right number
  of wrong rows come back.

* **§3 Role gate.** Unlike the PO and vendor restore views, this one carries a gate,
  mirroring ``deps.require_role([ADMIN, MANAGER])`` clause for clause — including its
  two short-circuits, ``is_superuser`` (whatever the role column says) and
  PLATFORM_ADMIN. Miss either and the same person who can DELETE a work order is 403'd
  out of the only view that would let them undo it. Every 403 here is anchored on a 200
  from the SAME user against the live list, so a crashed handler cannot pass as correct
  gating.

* **§4 The status carve-out.** The live list excludes COMPLETE/CLOSED/CANCELLED; the
  archive deliberately does NOT. ``WorkOrder.soft_delete`` leaves ``status`` untouched,
  so a finished-then-deleted job keeps its terminal status — and those are among the
  likeliest things somebody wants back. Fold the ``elif not deleted_only`` back into a
  plain ``else`` and the archive is empty exactly when someone needs it, with no other
  API surface able to reach those rows. The paired control test asserts the live view
  STILL applies the exclusion, so "just delete the filter" cannot pass this file.

* **§5 ``?status=`` still narrows the deleted view** — the carve-out suppresses the
  DEFAULT exclusion only, not an explicit filter.

* **§6 Provenance.** ``is_deleted`` / ``deleted_at`` / ``deleted_by_name``, resolved
  through the real ``DELETE /work-orders/{id}`` so the columns are written by production
  code. ``deleted_by`` is a bare Integer with no FK, resolved in one batched query with
  NO ``is_active`` filter: provenance must survive the deleter's departure, and the
  deletes people ask about most are the ones done by someone who has since left.

* **§7 The tri-state.** ``WorkOrderSummary`` sets ``from_attributes`` and
  ``is_deleted``/``deleted_at`` are REAL columns on ``WorkOrder``, so a future
  ``model_validate(orm_row)`` "simplification" of the one hand-built construction site
  would make every LIVE row answer ``is_deleted: false``. A shared row renderer reads
  non-null as "came from the restore view" and would offer Restore on a job nobody
  deleted. Asserted as ``is None``, never as falsy — falsy passes against exactly that
  bug.

* **§8 The default path is unchanged**, and ``deleted_only`` WINS over
  ``include_deleted`` when both are passed.

* **§9 The reconcile carve-out.** ``reconcile_work_orders_from_completion_evidence``
  carries no ``is_deleted`` predicate anywhere — it has never been handed a deleted row
  because this list never returned one. Run it over the archive and merely OPENING the
  restore screen drives a soft-deleted RELEASED job to COMPLETE from stale labor
  evidence, writing audit rows and firing inventory effects against a job somebody
  deleted, from a GET, with no actor intent. Pinned against a live positive control that
  proves the reconcile really does fire on the default path.
"""

from datetime import datetime

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import User, UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderStatus
from tests.lean_phase1_helpers import (
    COMPANY_A,
    COMPANY_B,
    headers_for,
    make_entry,
    make_op,
    make_part,
    make_user,
    make_wo,
    make_work_center,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

WORK_ORDERS_URL = "/api/v1/work-orders/"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _numbers(resp) -> set:
    """The work-order numbers in a list response body."""
    return {row["work_order_number"] for row in resp.json()}


def _row(resp, wo: WorkOrder) -> dict:
    """The one row for ``wo``, asserting it is present."""
    rows = {row["work_order_number"]: row for row in resp.json()}
    assert wo.work_order_number in rows, f"{wo.work_order_number} missing from {sorted(rows)}"
    return rows[wo.work_order_number]


def _delete_via_api(client: TestClient, wo: WorkOrder, actor: User) -> None:
    """Soft-delete through the real endpoint, so ``deleted_by``/``deleted_at`` are
    written by production code rather than by the test reaching for the mixin."""
    resp = client.delete(f"{WORK_ORDERS_URL}{wo.id}", headers=headers_for(actor))
    assert resp.status_code == status.HTTP_204_NO_CONTENT, resp.text


def _soft_delete_direct(db: Session, wo: WorkOrder, *, user_id: int = None) -> WorkOrder:
    """Soft-delete a row directly — for the cross-tenant fixture, where the deleting
    actor's company is the thing under test and the endpoint would refuse anyway."""
    wo.soft_delete(user_id)
    db.commit()
    db.refresh(wo)
    return wo


def _superuser(db: Session, *, role: UserRole = UserRole.OPERATOR) -> User:
    """A user whose ROLE is outside the gate but who carries ``is_superuser``.
    ``require_role`` short-circuits on this before the role test; the archive gate must
    too, or a superuser can delete a work order and then not find it."""
    user = make_user(db, role=role)
    user.is_superuser = True
    db.commit()
    db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# §1 The two views partition one fixture set
# ---------------------------------------------------------------------------


class TestPartition:
    def test_deleted_only_returns_the_deleted_rows_and_none_of_the_live_ones(
        self, client: TestClient, db_session: Session
    ):
        """Asserted BOTH directions off one fixture set. A one-way assertion ("the
        deleted one is there") passes against a ``deleted_only`` that ignores its own
        predicate and returns the whole table."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        part = make_part(db_session)
        live = make_wo(db_session, part, status_=WorkOrderStatus.RELEASED)
        gone = make_wo(db_session, part, status_=WorkOrderStatus.RELEASED)
        _delete_via_api(client, gone, admin)

        archive = client.get(f"{WORK_ORDERS_URL}?deleted_only=true", headers=headers_for(admin))
        assert archive.status_code == status.HTTP_200_OK, archive.text
        archived = _numbers(archive)
        assert gone.work_order_number in archived
        assert live.work_order_number not in archived

        default = client.get(WORK_ORDERS_URL, headers=headers_for(admin))
        assert default.status_code == status.HTTP_200_OK, default.text
        listed = _numbers(default)
        assert live.work_order_number in listed
        assert gone.work_order_number not in listed


# ---------------------------------------------------------------------------
# §2 Tenancy (invariant 1) — the security-defect case
# ---------------------------------------------------------------------------


class TestTenancy:
    def test_another_companys_deleted_work_order_is_never_returned(self, client: TestClient, db_session: Session):
        """The archive is still ``company_id``-scoped. Asserted on the OTHER tenant's
        specific work-order number (and anchored on our own being present), never on a
        count — a count assertion passes when the right NUMBER of wrong rows come back."""
        admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
        admin_b = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_B)

        part_a = make_part(db_session, company_id=COMPANY_A)
        part_b = make_part(db_session, company_id=COMPANY_B)
        mine = make_wo(db_session, part_a, company_id=COMPANY_A)
        theirs = make_wo(db_session, part_b, company_id=COMPANY_B)
        _delete_via_api(client, mine, admin_a)
        _delete_via_api(client, theirs, admin_b)

        resp = client.get(f"{WORK_ORDERS_URL}?deleted_only=true", headers=headers_for(admin_a))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        archived = _numbers(resp)
        assert mine.work_order_number in archived, "own deleted work order missing — the anchor failed"
        assert theirs.work_order_number not in archived, "CROSS-TENANT LEAK on the deleted view"

        # ...and symmetrically, so the test cannot pass because B's row simply never
        # got deleted.
        other_side = client.get(f"{WORK_ORDERS_URL}?deleted_only=true", headers=headers_for(admin_b))
        assert other_side.status_code == status.HTTP_200_OK, other_side.text
        assert _numbers(other_side) == {theirs.work_order_number}

    def test_a_deleted_row_of_another_tenant_is_not_reachable_by_status_filter_either(
        self, client: TestClient, db_session: Session
    ):
        """``status`` is shared by both views; it must not become a second door into
        another tenant's archive."""
        admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
        part_b = make_part(db_session, company_id=COMPANY_B)
        theirs = _soft_delete_direct(
            db_session, make_wo(db_session, part_b, company_id=COMPANY_B, status_=WorkOrderStatus.RELEASED)
        )

        resp = client.get(f"{WORK_ORDERS_URL}?deleted_only=true&status=released", headers=headers_for(admin_a))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert theirs.work_order_number not in _numbers(resp)


# ---------------------------------------------------------------------------
# §3 The role gate
# ---------------------------------------------------------------------------


class TestRoleGate:
    @pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER])
    def test_the_delete_restore_population_gets_200(self, client: TestClient, db_session: Session, role: UserRole):
        """ADMIN and MANAGER are exactly who ``require_role`` admits on
        ``DELETE /work-orders/{id}`` and ``POST /work-orders/{id}/restore``. A manager
        who may restore has to be able to find what to restore."""
        user = make_user(db_session, role=role)
        resp = client.get(f"{WORK_ORDERS_URL}?deleted_only=true", headers=headers_for(user))
        assert resp.status_code == status.HTTP_200_OK, resp.text

    @pytest.mark.parametrize(
        "role",
        [UserRole.SUPERVISOR, UserRole.OPERATOR, UserRole.QUALITY, UserRole.SHIPPING, UserRole.VIEWER],
    )
    def test_everyone_else_gets_403_but_can_still_read_the_live_list(
        self, client: TestClient, db_session: Session, role: UserRole
    ):
        """Every negative is anchored on a positive: the SAME user must get 200 from the
        live list. Without that anchor a handler that 500s or 403s on every call would
        pass this as correct gating."""
        user = make_user(db_session, role=role)

        refused = client.get(f"{WORK_ORDERS_URL}?deleted_only=true", headers=headers_for(user))
        assert refused.status_code == status.HTTP_403_FORBIDDEN, refused.text

        allowed = client.get(WORK_ORDERS_URL, headers=headers_for(user))
        assert allowed.status_code == status.HTTP_200_OK, allowed.text

    def test_a_superuser_outside_the_role_set_is_admitted(self, client: TestClient, db_session: Session):
        """``require_role`` returns on ``is_superuser`` BEFORE the role test, so an
        OPERATOR-roled superuser can delete a work order. Gate the archive on the two
        roles alone and that person can never see what they deleted."""
        user = _superuser(db_session, role=UserRole.OPERATOR)
        resp = client.get(f"{WORK_ORDERS_URL}?deleted_only=true", headers=headers_for(user))
        assert resp.status_code == status.HTTP_200_OK, resp.text

    def test_a_platform_admin_is_admitted(self, client: TestClient, db_session: Session):
        """The second ``require_role`` short-circuit. PLATFORM_ADMIN is not in
        ``[ADMIN, MANAGER]``, so a bare membership test would 403 it."""
        user = make_user(db_session, role=UserRole.PLATFORM_ADMIN)
        resp = client.get(f"{WORK_ORDERS_URL}?deleted_only=true", headers=headers_for(user))
        assert resp.status_code == status.HTTP_200_OK, resp.text

    def test_the_gate_refuses_before_any_row_is_read(self, client: TestClient, db_session: Session):
        """A refused archive read must not have touched the database — the reconcile-on-
        read commits, so a gate placed after the query would let a 403'd caller still
        drive state changes."""
        operator = make_user(db_session, role=UserRole.OPERATOR)
        part = make_part(db_session)
        wo = make_wo(db_session, part, status_=WorkOrderStatus.RELEASED)
        _soft_delete_direct(db_session, wo)

        refused = client.get(f"{WORK_ORDERS_URL}?deleted_only=true", headers=headers_for(operator))
        assert refused.status_code == status.HTTP_403_FORBIDDEN
        assert refused.json()["detail"] == "Insufficient permissions"

        db_session.rollback()
        db_session.expire_all()
        assert db_session.query(WorkOrder).filter(WorkOrder.id == wo.id).one().status == WorkOrderStatus.RELEASED


# ---------------------------------------------------------------------------
# §4 The status carve-out — and its control
# ---------------------------------------------------------------------------


class TestStatusCarveOut:
    @pytest.mark.parametrize(
        "wo_status",
        [WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED],
    )
    def test_a_deleted_terminal_work_order_is_still_in_the_archive(
        self, client: TestClient, db_session: Session, wo_status: WorkOrderStatus
    ):
        """THE regression that would make the whole screen useless. The default list
        excludes complete/closed/cancelled; the archive must not, because
        ``soft_delete`` leaves ``status`` untouched and a finished job that was deleted
        by mistake is exactly what someone comes here to get back. Hidden here it is
        reachable from NO API surface at all — the detail read 404s on a deleted row."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        part = make_part(db_session)
        wo = make_wo(db_session, part, status_=wo_status)
        _delete_via_api(client, wo, admin)

        resp = client.get(f"{WORK_ORDERS_URL}?deleted_only=true", headers=headers_for(admin))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert wo.work_order_number in _numbers(resp)

    def test_the_live_list_still_excludes_terminal_work_orders(self, client: TestClient, db_session: Session):
        """The control that stops "just delete the status filter" passing the test
        above. If this and the carve-out test are ever both green only because the
        exclusion was removed outright, this one fails first."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        part = make_part(db_session)
        done = make_wo(db_session, part, status_=WorkOrderStatus.COMPLETE)
        cancelled = make_wo(db_session, part, status_=WorkOrderStatus.CANCELLED)
        closed = make_wo(db_session, part, status_=WorkOrderStatus.CLOSED)
        live = make_wo(db_session, part, status_=WorkOrderStatus.RELEASED)

        resp = client.get(WORK_ORDERS_URL, headers=headers_for(admin))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        listed = _numbers(resp)
        assert live.work_order_number in listed
        for hidden in (done, cancelled, closed):
            assert hidden.work_order_number not in listed


# ---------------------------------------------------------------------------
# §5 An explicit ?status= still narrows the deleted view
# ---------------------------------------------------------------------------


class TestExplicitStatusNarrowsTheArchive:
    def test_status_filter_applies_to_the_deleted_view(self, client: TestClient, db_session: Session):
        """The carve-out suppresses the DEFAULT exclusion only. An explicit ``status``
        is the branch both views share, and it must keep working here or the archive
        becomes unfilterable the moment a shop has more than a screenful of tombstones."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        part = make_part(db_session)
        done = make_wo(db_session, part, status_=WorkOrderStatus.COMPLETE)
        released = make_wo(db_session, part, status_=WorkOrderStatus.RELEASED)
        _delete_via_api(client, done, admin)
        _delete_via_api(client, released, admin)

        both = client.get(f"{WORK_ORDERS_URL}?deleted_only=true", headers=headers_for(admin))
        assert _numbers(both) == {done.work_order_number, released.work_order_number}

        narrowed = client.get(f"{WORK_ORDERS_URL}?deleted_only=true&status=complete", headers=headers_for(admin))
        assert narrowed.status_code == status.HTTP_200_OK, narrowed.text
        assert _numbers(narrowed) == {done.work_order_number}


# ---------------------------------------------------------------------------
# §6 Provenance
# ---------------------------------------------------------------------------


class TestProvenance:
    def test_the_three_fields_are_populated_on_the_deleted_view(self, client: TestClient, db_session: Session):
        """Without a resolvable name the archive says only that *somebody* deleted the
        job — which is the one question a reader deciding whether to restore it asks."""
        deleter = make_user(db_session, role=UserRole.MANAGER, first_name="Dana", last_name="Deleter")
        admin = make_user(db_session, role=UserRole.ADMIN)
        part = make_part(db_session)
        wo = make_wo(db_session, part, status_=WorkOrderStatus.RELEASED)
        _delete_via_api(client, wo, deleter)

        resp = client.get(f"{WORK_ORDERS_URL}?deleted_only=true", headers=headers_for(admin))
        row = _row(resp, wo)
        assert row["is_deleted"] is True
        assert row["deleted_by_name"] == "Dana Deleter"
        # UTCModel: UTC ISO-8601 with a trailing Z, so the client can render Central
        # without guessing a zone.
        assert isinstance(row["deleted_at"], str) and row["deleted_at"].endswith("Z"), row["deleted_at"]
        assert datetime.fromisoformat(row["deleted_at"].replace("Z", "+00:00")) is not None

    def test_the_name_survives_the_deleters_departure(self, client: TestClient, db_session: Session):
        """The lookup deliberately applies NO ``is_active`` filter to ``User``.
        Provenance must outlive the deleter, and the deletes people ask about most are
        precisely the ones done by someone who has since left — filter them and the name
        blanks exactly when it is needed."""
        deleter = make_user(db_session, role=UserRole.MANAGER, first_name="Gone", last_name="Away")
        admin = make_user(db_session, role=UserRole.ADMIN)
        part = make_part(db_session)
        wo = make_wo(db_session, part, status_=WorkOrderStatus.RELEASED)
        _delete_via_api(client, wo, deleter)

        deleter.is_active = False
        db_session.commit()

        resp = client.get(f"{WORK_ORDERS_URL}?deleted_only=true", headers=headers_for(admin))
        assert _row(resp, wo)["deleted_by_name"] == "Gone Away"

    def test_deleted_by_name_is_none_when_the_user_row_is_gone(self, client: TestClient, db_session: Session):
        """``deleted_by`` is a bare Integer with no FK, so a hard-deleted user leaves a
        dangling id. That must degrade to ``None``, not 500 the archive."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        part = make_part(db_session)
        wo = make_wo(db_session, part, status_=WorkOrderStatus.RELEASED)
        _soft_delete_direct(db_session, wo, user_id=9_999_999)

        resp = client.get(f"{WORK_ORDERS_URL}?deleted_only=true", headers=headers_for(admin))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        row = _row(resp, wo)
        assert row["is_deleted"] is True
        assert row["deleted_by_name"] is None


# ---------------------------------------------------------------------------
# §7 The tri-state
# ---------------------------------------------------------------------------


class TestTriState:
    def test_the_default_list_reports_none_not_false(self, client: TestClient, db_session: Session):
        """``is None``, NEVER a falsy check — falsy passes against the exact bug this
        guards. ``WorkOrderSummary`` sets ``from_attributes`` and ``is_deleted`` /
        ``deleted_at`` are real ``SoftDeleteMixin`` columns, so replacing the hand-built
        kwargs with ``model_validate(wo)`` would silently make every live row answer
        ``is_deleted: false`` — which a shared renderer reads as "came from the restore
        view" and answers with a Restore button on a job nobody deleted."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        part = make_part(db_session)
        live = make_wo(db_session, part, status_=WorkOrderStatus.RELEASED)

        resp = client.get(WORK_ORDERS_URL, headers=headers_for(admin))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        rows = resp.json()
        assert rows, "no rows — the tri-state assertion below would be vacuous"
        assert live.work_order_number in _numbers(resp)
        for row in rows:
            assert "is_deleted" in row and row["is_deleted"] is None, row
            assert "deleted_at" in row and row["deleted_at"] is None, row
            assert "deleted_by_name" in row and row["deleted_by_name"] is None, row

    def test_include_deleted_rows_also_report_none(self, client: TestClient, db_session: Session):
        """``include_deleted`` is UNCHANGED by this feature. Its rows stay
        indistinguishable from live ones in the payload, exactly as today: non-null
        provenance means "came from the restore view", nothing else."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        part = make_part(db_session)
        gone = make_wo(db_session, part, status_=WorkOrderStatus.RELEASED)
        _delete_via_api(client, gone, admin)

        resp = client.get(f"{WORK_ORDERS_URL}?include_deleted=true", headers=headers_for(admin))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        row = _row(resp, gone)
        assert row["is_deleted"] is None
        assert row["deleted_at"] is None
        assert row["deleted_by_name"] is None

    def test_a_status_filtered_default_read_also_reports_none(self, client: TestClient, db_session: Session):
        """The ternaries key on ``deleted_only``, not on the row's own column, so no
        other combination of params may leak a non-null."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        part = make_part(db_session)
        make_wo(db_session, part, status_=WorkOrderStatus.COMPLETE)

        resp = client.get(f"{WORK_ORDERS_URL}?status=complete", headers=headers_for(admin))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json(), "no rows — assertion would be vacuous"
        for row in resp.json():
            assert row["is_deleted"] is None, row


# ---------------------------------------------------------------------------
# §8 The default path is unchanged; deleted_only wins over include_deleted
# ---------------------------------------------------------------------------


class TestDefaultPathAndPrecedence:
    def test_deleted_only_false_matches_the_unset_default_byte_for_byte(self, client: TestClient, db_session: Session):
        """The flag must be provably inert when off — same rows, same order, same
        bytes. Anything else means the new branch changed the live list."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        part = make_part(db_session)
        make_wo(db_session, part, status_=WorkOrderStatus.RELEASED)
        make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
        gone = make_wo(db_session, part, status_=WorkOrderStatus.RELEASED)
        _delete_via_api(client, gone, admin)

        unset = client.get(WORK_ORDERS_URL, headers=headers_for(admin))
        explicit_false = client.get(f"{WORK_ORDERS_URL}?deleted_only=false", headers=headers_for(admin))
        assert unset.status_code == status.HTTP_200_OK, unset.text
        assert explicit_false.status_code == status.HTTP_200_OK, explicit_false.text
        assert unset.json() == explicit_false.json()
        assert gone.work_order_number not in _numbers(unset)

    def test_deleted_only_wins_when_include_deleted_is_also_passed(self, client: TestClient, db_session: Session):
        """``deleted_only`` is the narrower, explicit view. Fold it into the same branch
        as ``include_deleted`` and an ADMIN passing both gets the UNION (live + deleted)
        — an archive with live jobs in it, each carrying a Restore control."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        part = make_part(db_session)
        live = make_wo(db_session, part, status_=WorkOrderStatus.RELEASED)
        gone = make_wo(db_session, part, status_=WorkOrderStatus.RELEASED)
        _delete_via_api(client, gone, admin)

        resp = client.get(
            f"{WORK_ORDERS_URL}?deleted_only=true&include_deleted=true",
            headers=headers_for(admin),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        listed = _numbers(resp)
        assert gone.work_order_number in listed
        assert live.work_order_number not in listed
        # ...and the archive's provenance still fills in, i.e. it really is the deleted
        # view and not include_deleted wearing its name.
        assert _row(resp, gone)["is_deleted"] is True

    def test_a_manager_passing_both_gets_the_archive_not_a_403_or_the_live_list(
        self, client: TestClient, db_session: Session
    ):
        """``include_deleted`` is ADMIN-only (a bare role equality); ``deleted_only`` is
        ADMIN+MANAGER. A manager passing both must land in the archive — precedence has
        to be decided before the ADMIN-only branch, not by it."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        part = make_part(db_session)
        live = make_wo(db_session, part, status_=WorkOrderStatus.RELEASED)
        gone = make_wo(db_session, part, status_=WorkOrderStatus.RELEASED)
        _delete_via_api(client, gone, manager)

        resp = client.get(
            f"{WORK_ORDERS_URL}?deleted_only=true&include_deleted=true",
            headers=headers_for(manager),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        listed = _numbers(resp)
        assert gone.work_order_number in listed
        assert live.work_order_number not in listed


# ---------------------------------------------------------------------------
# §9 The reconcile-on-read carve-out
# ---------------------------------------------------------------------------


def _wo_with_completion_evidence(db: Session, actor: User) -> WorkOrder:
    """A RELEASED WO whose closed labor evidence covers the whole order — the shape the
    read-path reconcile drives to COMPLETE."""
    part = make_part(db)
    wc = make_work_center(db)
    wo = make_wo(db, part, status_=WorkOrderStatus.RELEASED, quantity_ordered=10)
    op = make_op(db, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_entry(db, actor, wo, op, wc, quantity_produced=10)
    return wo


class TestReconcileIsNotRunOverTheArchive:
    def test_the_live_list_does_reconcile_this_shape(self, client: TestClient, db_session: Session):
        """The positive control. Without it the carve-out test below would pass against
        a fixture the reconcile never touches in the first place."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        wo = _wo_with_completion_evidence(db_session, admin)

        resp = client.get(WORK_ORDERS_URL, headers=headers_for(admin))
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.rollback()
        db_session.expire_all()
        reread = db_session.query(WorkOrder).filter(WorkOrder.id == wo.id).one()
        assert reread.status == WorkOrderStatus.COMPLETE, "the reconcile did not fire — the control is broken"

    def test_opening_the_archive_does_not_reconcile_a_deleted_work_order(self, client: TestClient, db_session: Session):
        """``reconcile_work_orders_from_completion_evidence`` carries NO ``is_deleted``
        predicate — it has only ever been safe because this list never returned a
        deleted row. Run it over the archive and merely OPENING the restore screen
        drives a deleted RELEASED job to COMPLETE from stale labor evidence, writes
        audit rows, refreshes scheduling and fires the FG receipt + gated backflush:
        inventory movements against a job somebody deleted, caused by a GET, with no
        actor intent and no reason recorded."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        wo = _wo_with_completion_evidence(db_session, admin)
        _delete_via_api(client, wo, admin)

        resp = client.get(f"{WORK_ORDERS_URL}?deleted_only=true", headers=headers_for(admin))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert wo.work_order_number in _numbers(resp), "fixture missing from the archive — assertion would be vacuous"
        assert _row(resp, wo)["status"] == WorkOrderStatus.RELEASED.value

        db_session.rollback()
        db_session.expire_all()
        reread = db_session.query(WorkOrder).filter(WorkOrder.id == wo.id).one()
        assert reread.status == WorkOrderStatus.RELEASED, "the archive read reconciled a DELETED work order"
        assert reread.is_deleted is True
