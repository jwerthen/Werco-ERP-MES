"""The vendor restore view (``?deleted_only=true``) and the is_active-preserving restore.

Two halves of one PR, and they exist together because neither is useful alone.

**Half A -- discovery.** ``POST /purchasing/vendors/{id}/restore`` has existed since the
soft-delete work landed, but nothing could reach it: ``GET /purchasing/vendors`` hard-
filtered ``is_deleted == False`` with no override, so a soft-deleted vendor was invisible
to every API caller and ``restoreVendor`` sat in the frontend client with zero call sites.
That was survivable while a deleted vendor was merely hidden; it stopped being survivable
when the vendor-read tightening made a soft-deleted vendor UNRESOLVABLE on five write
paths. Those refusals shipped with no way to undo a delete. ``deleted_only`` is the
missing half. Mirrors the PO twin -- see ``test_purchase_order_delete_restore.py``.

**Half B -- restore preserves ``is_active``.** ``delete_vendor`` forces ``is_active=False``
on the way out (a deliberate second layer behind the ``is_deleted`` filters -- it stays),
so restore had to decide what to put back and used to hard-code ``True``. An
approved-supplier list is an AS9100D-controlled artifact: a supplier the shop deliberately
DEACTIVATED and then deleted must not come back looking active and selectable just because
somebody undid a delete. ``Vendor.is_active_before_delete`` (migration 082) remembers the
pre-delete value; restore puts it back and clears the sidecar.

What each section pins:

- §1  the two views partition ONE fixture set (both directions, so an endpoint that
      ignored the flag and returned everything could not pass)
- §2  the flag is provably inert when unset -- same BYTES, not merely the same rows,
      and no extra query either (the name resolution is one batched lookup, and it
      does not run at all on the default path)
- §3  THE CARVE-OUT. ``active_only`` defaults to True and the delete writes the very
      column it filters, so ANDing them empties the restore view by construction. Plus
      the control that stops "delete the active_only filter" from passing instead.
- §4  ``approved_only`` deliberately KEEPS applying to the deleted view (it is neither
      half of the trap: never silently on, never written by the delete)
- §5  the default path is unchanged -- rows, order, and the tri-state nulls on all four
      ``VendorResponse`` endpoints
- §6  the provenance fields the restore decision is actually made from
- §7  THE OWNER'S DECISION, in both directions plus the legacy and repeat cases
- §8  tenant isolation on a view over previously-invisible rows (invariant 1)
- §9  the read-tightening guards this PR must not break (restore/delete/code probes)
- §10 role posture: reading the restore view is not gated, restoring is
- §11 the end-to-end point: delete -> a refusal -> restore -> the refusal lifts. And its
      security counterpart: restoring a DEACTIVATED vendor does NOT lift it.
"""

from datetime import datetime

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.purchasing import POStatus, PurchaseOrder, Vendor
from app.models.user import UserRole
from tests.api.test_receiving_compliance import _next, headers_for, make_po_line, make_user
from tests.api.test_vendor_delete_restore import make_vendor

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

VENDORS = "/api/v1/purchasing/vendors"
PO_BASE = "/api/v1/purchasing/purchase-orders"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vendor(
    db: Session,
    *,
    company_id: int = 1,
    is_active: bool = True,
    is_approved: bool = True,
    name: str = None,
) -> Vendor:
    """A PO-free vendor with the two flags this file cares about under test control.

    Wraps the shared ``make_vendor`` factory (globally-unique ``code`` from the module
    counter) rather than re-rolling one, so a change to the fixture shape propagates.
    """
    vendor = make_vendor(db, company_id=company_id, is_active=is_active)
    if name is not None or not is_approved:
        if name is not None:
            vendor.name = name
        vendor.is_approved = is_approved
        db.commit()
        db.refresh(vendor)
    return vendor


def _list(client: TestClient, user, **params) -> list:
    resp = client.get(VENDORS, headers=headers_for(user), params=params)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()


def _codes(rows: list) -> list:
    """Vendor codes in response order. Assert on the natural key, never on a count --
    the suite runs on a per-worker DB and rows from a sibling fixture must not be able
    to turn a real regression into a passing count."""
    return [r["code"] for r in rows]


def _delete(client: TestClient, user, vendor_id: int) -> None:
    resp = client.delete(f"{VENDORS}/{vendor_id}", headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text


def _restore(client: TestClient, user, vendor_id: int) -> None:
    resp = client.post(f"{VENDORS}/{vendor_id}/restore", headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text


def _row(db: Session, vendor_id: int) -> Vendor:
    """Re-read the row from the DB. The TestClient shares this session, so the identity
    map must be expired or an endpoint's write is invisible here."""
    db.expire_all()
    return db.query(Vendor).filter(Vendor.id == vendor_id).one()


def _restore_audit_rows(db: Session, vendor_id: int):
    # AuditService.log_update(action="restore") stores the verb upper-cased.
    return [
        log
        for log in db.query(AuditLog).all()
        if log.resource_type == "vendor" and log.action == "RESTORE" and log.resource_id == vendor_id
    ]


def _po_body(vendor_id: int, part_id: int) -> dict:
    return {
        "vendor_id": vendor_id,
        "lines": [{"part_id": part_id, "quantity_ordered": 10, "unit_price": 5.0}],
    }


# ===========================================================================
# §1. Both directions, one fixture set
# ===========================================================================


def test_deleted_only_partitions_the_same_fixture_set(client: TestClient, db_session: Session):
    """The two views must be complementary, asserted against ONE fixture set.

    Checking only that ``deleted_only=true`` returns the deleted vendor would pass for an
    endpoint that ignores the flag entirely and returns everything, so both directions are
    asserted here: each vendor appears in exactly one of the two views.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    live = _vendor(db_session)
    doomed = _vendor(db_session)
    live_code, doomed_code = live.code, doomed.code

    _delete(client, admin, doomed.id)

    default_view = _codes(_list(client, admin))
    deleted_view = _codes(_list(client, admin, deleted_only=True))

    # Live vendor: default view only.
    assert live_code in default_view
    assert live_code not in deleted_view
    # Deleted vendor: restore view only.
    assert doomed_code in deleted_view
    assert doomed_code not in default_view
    # Disjoint.
    assert set(default_view) & set(deleted_view) == set()


# ===========================================================================
# §2. The parameter is inert when unset (frozen contract)
# ===========================================================================


def test_deleted_only_false_is_byte_identical_to_unset(client: TestClient, db_session: Session):
    """``deleted_only=false`` and no parameter at all must produce the SAME response body
    -- not "the same rows", the same bytes -- so the flag cannot have introduced a second
    code path that merely happens to agree today."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    _vendor(db_session, name="Alpha Metals")
    _vendor(db_session, name="Bravo Steel")
    doomed = _vendor(db_session, name="Charlie Alloys")
    _delete(client, admin, doomed.id)

    unset = client.get(VENDORS, headers=headers_for(admin))
    explicit_false = client.get(VENDORS, headers=headers_for(admin), params={"deleted_only": False})

    assert unset.status_code == status.HTTP_200_OK, unset.text
    assert explicit_false.status_code == status.HTTP_200_OK, explicit_false.text
    assert unset.json() == explicit_false.json()


def test_name_resolution_is_one_batched_query_and_none_on_the_default_path(client: TestClient, db_session: Session):
    """The ``deleted_by`` -> name lookup must cost ONE query on the deleted view and ZERO
    on the default one. Neither is observable from a response body -- ``_vendor_response``
    nulls the three fields on the default path regardless -- so a statement count is the
    only thing that can see either regression, and nothing else in this file does.

    Verified by mutation; what each half actually catches:

    * ONE on the deleted view catches the N+1. Resolving per row is the tempting refactor
      (``deleted_by`` is a bare Integer with no FK, so there is nothing to joinedload and
      a lookup-per-row reads as the natural shape). Three deleted vendors under TWO
      distinct deleters make it observable: a per-row loop emits three statements and
      cannot collapse to one by coincidence. Confirmed to fail against a per-row rewrite.

    * ZERO on the default path catches an unconditional-resolution refactor -- the kind
      that lifts this block into a shared helper called from both views. Confirmed to fail
      against that (the query goes out with an empty ``IN``).

      It does NOT fail if only the ``if deleted_only:`` guard is removed, and that is worth
      knowing rather than papering over: the default path is query-free for TWO independent
      reasons, and the inner ``if deleter_ids:`` is the one that holds. ``SoftDeleteMixin.
      restore()`` sets ``deleted_by = None``, so a LIVE vendor never carries a deleter id,
      the set is empty by construction on that path, and the inner guard short-circuits.
      The outer guard is the statement of intent; the inner one is the belt. Should
      ``restore()`` ever stop clearing ``deleted_by``, this assertion starts catching the
      outer guard too -- which is the right way round.

    The resolution statement is matched by its distinctive projection rather than by
    counting ``users`` traffic generally. Every authenticated request also resolves the
    CALLER (``get_current_user``), and that traffic is not constant between two requests
    on a warm session -- a bare "count statements mentioning users" assertion measures the
    auth path as much as this feature, and drifts. The resolution query selects exactly
    id/first_name/last_name; the auth query loads the whole ORM row, ``hashed_password``
    included. That is the discriminator.
    """
    from sqlalchemy import event

    # Listen on the SESSION'S OWN bind, never on a conftest-level ``engine`` import.
    # conftest is loaded once as pytest's rootdir plugin and AGAIN as ``tests.conftest``
    # if anything imports it, which builds a second engine object against the same
    # shared-cache in-memory URL: the DB still works, the listener fires on nothing, and
    # the test passes vacuously with zero statements on both sides. The ``client`` fixture
    # overrides ``get_db`` to yield this very session, so this bind is the one the request
    # actually executes on. (Verified by observation, not assumed -- the two engine objects
    # really are distinct under this suite's layout.)
    bind = db_session.get_bind()

    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    admin_b = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    first = _vendor(db_session)
    second = _vendor(db_session)
    third = _vendor(db_session)

    _delete(client, admin_a, first.id)
    _delete(client, admin_a, second.id)
    _delete(client, admin_b, third.id)

    captured: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        captured.append(" ".join(statement.split()))

    def _statements(**params) -> list[str]:
        """Every statement emitted while serving ONE list request."""
        captured.clear()
        event.listen(bind, "before_cursor_execute", _record)
        try:
            resp = client.get(VENDORS, headers=headers_for(admin_a), params=params)
        finally:
            event.remove(bind, "before_cursor_execute", _record)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        return list(captured)

    def _resolution_queries(statements: list[str]) -> list[str]:
        return [
            stmt
            for stmt in statements
            if "FROM users" in stmt and "users.first_name" in stmt and "users.hashed_password" not in stmt
        ]

    default_statements = _statements()
    deleted_statements = _statements(deleted_only=True)

    # Vacuous-pass guard: prove the listener is live before reading anything into a zero.
    assert any("FROM vendors" in stmt for stmt in default_statements), (
        "no vendor query captured at all -- the listener is attached to the wrong bind and "
        "both counts below would be a meaningless zero"
    )

    assert _resolution_queries(default_statements) == [], (
        "the default path resolved deleted_by -> name. The guard is what keeps the unset "
        "parameter inert; the wasted query is invisible in the response because "
        "_vendor_response nulls the fields either way"
    )

    resolution = _resolution_queries(deleted_statements)
    assert len(resolution) == 1, (
        f"expected ONE batched users lookup on the deleted view, got {len(resolution)} "
        f"for 3 deleted vendors under 2 deleters -- the resolution went per-row: {resolution}"
    )
    # Batched in shape as well as in count: one IN over the distinct deleter ids.
    assert " IN " in resolution[0], resolution[0]


# ===========================================================================
# §3. THE CARVE-OUT -- the one that would silently break the whole feature
# ===========================================================================


def test_deleted_view_ignores_the_active_only_default(client: TestClient, db_session: Session):
    """``active_only`` must NOT apply when ``deleted_only=true``.

    This is the vendor-specific trap and it is fatal if missed. ``list_vendors`` declares
    ``active_only: bool = True`` -- ON for every caller who never mentions it -- and
    ``delete_vendor`` sets ``is_active = False`` on its way out. A naive AND therefore
    emits ``is_deleted = true AND is_active = true``, which is unsatisfiable BY
    CONSTRUCTION: the restore screen reads "no deleted vendors" no matter how many exist,
    the Restore control can never be offered, and nothing else in the API can reach those
    rows either.

    Both vendors below were ACTIVE when deleted, so the only thing that can hide them is
    the mask the delete itself wrote. If someone "tidies" the ``and not deleted_only``
    away, this returns an empty list and this test fails.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    first = _vendor(db_session, is_active=True)
    second = _vendor(db_session, is_active=True)
    first_code, second_code = first.code, second.code

    _delete(client, admin, first.id)
    _delete(client, admin, second.id)

    # The default call: active_only is left at its default True, exactly as the restore
    # screen calls it.
    deleted_view = _codes(_list(client, admin, deleted_only=True))
    assert first_code in deleted_view, "active_only leaked into the deleted view -- the restore view is always empty"
    assert second_code in deleted_view

    # And explicitly passing the default must not change the answer either: the carve-out
    # is on the VIEW, not on whether the caller happened to name the parameter.
    explicit = _codes(_list(client, admin, deleted_only=True, active_only=True))
    assert first_code in explicit and second_code in explicit

    # active_only=false is likewise a no-op on this view (rather than an error or a
    # widening) -- the frontend must not have to know which value to send.
    relaxed = _codes(_list(client, admin, deleted_only=True, active_only=False))
    assert sorted(relaxed) == sorted(deleted_view)


def test_active_only_still_filters_the_live_view(client: TestClient, db_session: Session):
    """Control for the carve-out: ``active_only`` was skipped on the DELETED view only.

    Without this, "delete the active_only filter outright" satisfies the carve-out test
    above while silently widening the ordinary vendor picker to include every deactivated
    supplier -- which is a supplier-control regression, not a cosmetic one.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    active = _vendor(db_session, is_active=True)
    inactive = _vendor(db_session, is_active=False)

    default_view = _codes(_list(client, admin))
    assert active.code in default_view
    assert inactive.code not in default_view, "a deactivated vendor leaked into the default (active_only) list"

    relaxed = _codes(_list(client, admin, active_only=False))
    assert active.code in relaxed
    assert inactive.code in relaxed


# ===========================================================================
# §4. approved_only deliberately KEEPS applying to the deleted view
# ===========================================================================


def test_approved_only_still_narrows_the_deleted_view(client: TestClient, db_session: Session):
    """Documented decision, pinned so it is a choice rather than an oversight.

    ``approved_only`` is neither half of the ``active_only`` trap: it defaults to False
    (never silently on) and ``delete_vendor`` never touches ``is_approved`` (so it still
    means what it says on a deleted row and cannot empty the view behind the caller's
    back). Narrowing the restore view with an explicit ``approved_only=true`` is a real
    question with a real answer -- the same reason ``?status=`` is shared by both views of
    the PO list.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    approved = _vendor(db_session, is_approved=True)
    unapproved = _vendor(db_session, is_approved=False)
    approved_code, unapproved_code = approved.code, unapproved.code

    _delete(client, admin, approved.id)
    _delete(client, admin, unapproved.id)

    # Unnarrowed: both are restorable.
    unnarrowed = _codes(_list(client, admin, deleted_only=True))
    assert approved_code in unnarrowed and unapproved_code in unnarrowed

    # Explicitly narrowed: the filter still means what it says.
    narrowed = _codes(_list(client, admin, deleted_only=True, approved_only=True))
    assert approved_code in narrowed
    assert unapproved_code not in narrowed


# ===========================================================================
# §5. The default path is unchanged
# ===========================================================================


def test_default_view_rows_order_and_shape_unchanged(client: TestClient, db_session: Session):
    """A zero-argument request returns exactly the pre-feature result set, in order.

    The deleted vendor is deliberately named so it sorts FIRST under the endpoint's
    ``ORDER BY name``, so a broken soft-delete predicate puts it at the head of the list
    and fails loudly rather than hiding at the tail.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    _vendor(db_session, name="Charlie Alloys")
    _vendor(db_session, name="Alpha Metals")
    _vendor(db_session, name="Bravo Steel")
    doomed = _vendor(db_session, name="AAA Removed Supplier")

    _delete(client, admin, doomed.id)

    rows = _list(client, admin)
    assert [r["name"] for r in rows] == ["Alpha Metals", "Bravo Steel", "Charlie Alloys"]

    for row in rows:
        # The four new fields exist on the default path but assert NOTHING there --
        # the tri-state that lets one shared row renderer tell "restore view" from
        # "live vendor" without threading the query param through the UI.
        assert row["is_deleted"] is None
        assert row["deleted_at"] is None
        assert row["deleted_by_name"] is None
        # ...including the restore-preview field. A live vendor has no pending restore,
        # and leaking the sidecar here would let a renderer read a live row as archived.
        assert row["is_active_before_delete"] is None
        # Nothing else about the row shape moved.
        assert row["is_active"] is True
        assert row["code"]
        assert row["id"]


def test_provenance_fields_are_null_on_create_get_and_update(client: TestClient, db_session: Session):
    """``is_deleted`` / ``deleted_at`` are REAL COLUMNS on Vendor and ``VendorResponse``
    sets ``from_attributes``, so a returned ORM row would populate them automatically --
    ``GET /vendors/{id}`` answering ``is_deleted: false`` while the default list answers
    ``null``. A shared row renderer reading "non-null is_deleted" as "this came from the
    restore view" would then offer a Restore control on a LIVE vendor.

    All four VendorResponse endpoints must therefore serialize through the same helper.
    (The PO twin never had to solve this: its list rows are hand-built.)
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    code = f"VN{_next():05d}"

    created = client.post(
        VENDORS,
        headers=headers_for(admin),
        json={"code": code, "name": "Tri State Supplier"},
    )
    assert created.status_code == status.HTTP_200_OK, created.text
    vendor_id = created.json()["id"]

    fetched = client.get(f"{VENDORS}/{vendor_id}", headers=headers_for(admin))
    assert fetched.status_code == status.HTTP_200_OK, fetched.text

    updated = client.put(
        f"{VENDORS}/{vendor_id}",
        headers=headers_for(admin),
        json={"version": 0, "name": "Tri State Supplier Renamed"},
    )
    assert updated.status_code == status.HTTP_200_OK, updated.text

    for label, resp in (("create", created), ("get", fetched), ("update", updated)):
        body = resp.json()
        assert body["is_deleted"] is None, f"{label} asserted deletion state the default list leaves null"
        assert body["deleted_at"] is None, label
        assert body["deleted_by_name"] is None, label
        assert body["is_active_before_delete"] is None, label


# ===========================================================================
# §6. The provenance fields on the deleted view
# ===========================================================================


def test_deleted_view_populates_provenance_fields(client: TestClient, db_session: Session):
    """``is_deleted`` / ``deleted_at`` / ``deleted_by_name`` are what make the restore
    decision possible: WHEN the supplier went and WHO sent it there."""
    manager = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    vendor = _vendor(db_session)
    code = vendor.code

    _delete(client, manager, vendor.id)

    row = next(r for r in _list(client, manager, deleted_only=True) if r["code"] == code)
    assert row["is_deleted"] is True
    assert row["deleted_by_name"] == f"{manager.first_name} {manager.last_name}"

    # UTCModel contract: served as UTC ISO-8601 with a trailing 'Z'. SoftDeleteMixin
    # writes a NAIVE utcnow(), so without the UTC stamping this would ship without its Z
    # and utils/centralTime.ts would render it in the viewer's timezone.
    assert isinstance(row["deleted_at"], str)
    assert row["deleted_at"].endswith("Z"), row["deleted_at"]
    parsed = datetime.fromisoformat(row["deleted_at"].replace("Z", "+00:00"))
    assert parsed.utcoffset().total_seconds() == 0


def test_deleted_view_reports_what_a_restore_will_do_to_is_active(client: TestClient, db_session: Session):
    """``is_active_before_delete`` is the PRE-CLICK signal, and it is the only one there is.

    Restore preserves the pre-delete ``is_active``, so on a mixed archive some rows come
    back selectable and some come back switched off. The row's own ``is_active`` cannot
    tell them apart -- the delete forces it False on every deleted row -- so without this
    field the operator learns the outcome only AFTER acting. On an AS9100D-controlled
    approved-supplier list that is the "correct backend, invisible signal" failure.

    All three branches are asserted against ONE archive, because telling them apart is the
    whole job.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    was_active = _vendor(db_session, is_active=True)
    was_inactive = _vendor(db_session, is_active=False)
    legacy = _vendor(db_session, is_active=True)
    active_code, inactive_code, legacy_code = was_active.code, was_inactive.code, legacy.code
    active_id, inactive_id, legacy_id = was_active.id, was_inactive.id, legacy.id

    for vendor_id in (active_id, inactive_id, legacy_id):
        _delete(client, admin, vendor_id)

    # Age the third row into a pre-082 deletion: the column did not exist, so nothing was
    # captured. Done at the DB level because no request schema can seed the sidecar.
    row = _row(db_session, legacy_id)
    row.is_active_before_delete = None
    db_session.commit()

    archive = {r["code"]: r for r in _list(client, admin, deleted_only=True)}
    assert archive[active_code]["is_active_before_delete"] is True
    assert archive[inactive_code]["is_active_before_delete"] is False
    assert archive[legacy_code]["is_active_before_delete"] is None

    # Every deleted row reports is_active False regardless, which is exactly why the
    # sidecar has to be on the wire: this column cannot answer the question.
    assert {archive[c]["is_active"] for c in (active_code, inactive_code, legacy_code)} == {False}

    # And the field is not merely reported -- it PREDICTS. Restore each and check the
    # prediction held, so the two can never drift apart silently.
    for code, vendor_id, predicted in (
        (active_code, active_id, True),
        (inactive_code, inactive_id, False),
        (legacy_code, legacy_id, False),  # None predicts INACTIVE, not "unknown"
    ):
        _restore(client, admin, vendor_id)
        assert _row(db_session, vendor_id).is_active is predicted, code


def test_deleted_by_name_is_none_when_the_user_row_is_gone(client: TestClient, db_session: Session):
    """Contract: ``deleted_by_name`` is None if the deleter's user row no longer resolves
    -- the vendor must still be listable and restorable without a name."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = _vendor(db_session)
    vendor_id, code = vendor.id, vendor.code
    _delete(client, admin, vendor_id)

    # ``deleted_by`` is a bare Integer column with no FK, which is exactly why this state
    # is representable at all.
    row = _row(db_session, vendor_id)
    row.deleted_by = 999999
    db_session.commit()

    listed = next(r for r in _list(client, admin, deleted_only=True) if r["code"] == code)
    assert listed["deleted_by_name"] is None
    assert listed["is_deleted"] is True
    assert listed["deleted_at"] is not None


# ===========================================================================
# §7. THE OWNER'S DECISION: restore keeps the pre-delete is_active
# ===========================================================================


def test_delete_records_the_prior_value_then_forces_is_active_false(client: TestClient, db_session: Session):
    """ORDER IS LOAD-BEARING in ``delete_vendor``: capture the CURRENT ``is_active``
    into the sidecar FIRST, then force it False.

    Reversed, the sidecar records the value the very next line is about to write, is
    always False, and every restore reactivates -- the exact bug this column exists to
    prevent. The ``is_active = False`` write itself STAYS (it is a deliberate second layer
    behind the is_deleted filters); the fix is to remember, not to stop writing.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = _vendor(db_session, is_active=True)
    vendor_id = vendor.id

    _delete(client, admin, vendor_id)

    row = _row(db_session, vendor_id)
    assert row.is_deleted is True
    assert row.is_active is False, "the delete must still write the is_active mask -- that layer stays"
    assert row.is_active_before_delete is True, "the sidecar captured the post-delete value, not the pre-delete one"


def test_vendor_active_at_delete_restores_active(client: TestClient, db_session: Session):
    """The ordinary case: an active supplier deleted by mistake comes back usable."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = _vendor(db_session, is_active=True)
    vendor_id, code = vendor.id, vendor.code

    _delete(client, admin, vendor_id)
    _restore(client, admin, vendor_id)

    row = _row(db_session, vendor_id)
    assert row.is_deleted is False
    assert row.is_active is True

    # And it is selectable again through the ordinary (active_only) list.
    assert code in _codes(_list(client, admin))


def test_vendor_inactive_at_delete_restores_inactive(client: TestClient, db_session: Session):
    """THE HEADLINE. A supplier the shop DEACTIVATED and then deleted must come back
    deactivated.

    An approved-supplier list is an AS9100D-controlled artifact. Undoing a delete restores
    a RECORD; it is not an approval decision and must not silently make one. If someone
    reinstates the old unconditional ``is_active = True``, this test fails -- and so does
    the supplier-control posture it protects, because the vendor would immediately be
    selectable again in every picker without anyone approving it.

    Asserted at BOTH levels: the column, and the API behaviour that depends on it.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = _vendor(db_session, is_active=False)
    vendor_id, code = vendor.id, vendor.code

    _delete(client, admin, vendor_id)
    assert _row(db_session, vendor_id).is_active_before_delete is False

    _restore(client, admin, vendor_id)

    row = _row(db_session, vendor_id)
    assert row.is_deleted is False, "the restore must still undo the delete"
    assert row.is_active is False, "restore silently re-activated a deliberately deactivated supplier"

    # Behavioural half: back as a record (it resolves, and active_only=false lists it),
    # but NOT back on the selectable list.
    assert client.get(f"{VENDORS}/{vendor_id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK
    assert code in _codes(_list(client, admin, active_only=False))
    assert code not in _codes(_list(client, admin))
    # And no longer in the restore view -- the delete really was undone.
    assert code not in _codes(_list(client, admin, deleted_only=True))


def test_legacy_null_sidecar_restores_inactive(client: TestClient, db_session: Session):
    """A vendor deleted BEFORE migration 082 has ``is_active_before_delete IS NULL``, and
    by OWNER DECISION it restores INACTIVE.

    This is a DELIBERATE BREAK from the pre-082 unconditional ``is_active = True``, and it
    is the assertion someone will try to "fix" back. Do not. For a legacy row the system
    genuinely does not know whether the shop had switched the vendor off before deleting
    it: the delete overwrote the flag in place, and the ``audit_log`` delete row records
    the deletion, not the flag. On an AS9100D-controlled approved-supplier list the safe
    unknown is OFF -- the vendor comes back switched off and a human reactivates it
    deliberately, through the separately audited ``PUT /vendors/{id}``. Coming back
    inactive costs one audited reactivation; coming back wrongly active is not detectable
    at all, because nothing distinguishes it from a supplier that was always approved.

    Forward-only, no backfill: migration 082 adds the column nullable with no server
    default precisely so NULL stays reachable and keeps meaning "never recorded".

    The fixture is deliberately ACTIVE before the delete, which is what makes the NULL
    branch observable rather than coincidental -- preservation would have restored it
    True, so only the fallback can produce False here.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = _vendor(db_session, is_active=True)
    vendor_id, code = vendor.id, vendor.code

    _delete(client, admin, vendor_id)

    # Simulate a row deleted before the column existed. Done at the DB level on purpose:
    # no request schema exposes the sidecar (pinned two tests below), so this state is
    # only reachable the way a real legacy row got there -- a delete that predates 082.
    row = _row(db_session, vendor_id)
    assert row.is_active_before_delete is True, "fixture guard: the delete must have captured True"
    row.is_active_before_delete = None
    db_session.commit()

    _restore(client, admin, vendor_id)

    restored = _row(db_session, vendor_id)
    assert restored.is_deleted is False, "the restore must still undo the delete"
    assert restored.is_active is False, (
        "a pre-082 deletion restored ACTIVE -- the unconditional is_active = True was "
        "reinstated. The owner's decision is COALESCE(is_active_before_delete, FALSE): an "
        "unknown prior state on an approved-supplier list resolves OFF, never ON."
    )
    assert restored.is_active_before_delete is None

    # Behavioural half, so this is pinned at the API and not only at the column: the
    # record is back and resolvable, but it is NOT back on the selectable (active_only)
    # list until a human says so.
    assert client.get(f"{VENDORS}/{vendor_id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK
    assert code in _codes(_list(client, admin, active_only=False))
    assert code not in _codes(_list(client, admin))
    assert code not in _codes(_list(client, admin, deleted_only=True))


def test_restore_clears_the_sidecar(client: TestClient, db_session: Session):
    """The sidecar is only meaningful while the row is deleted, so restore clears it back
    to NULL. Asserted directly: a later cycle re-captures on the way out, so a stale value
    is only observable here."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = _vendor(db_session, is_active=True)
    vendor_id = vendor.id

    _delete(client, admin, vendor_id)
    assert _row(db_session, vendor_id).is_active_before_delete is True

    _restore(client, admin, vendor_id)
    assert _row(db_session, vendor_id).is_active_before_delete is None


def test_full_deactivate_delete_restore_cycle_is_repeatable(client: TestClient, db_session: Session):
    """deactivate -> delete -> restore -> reactivate -> delete -> restore.

    The second cycle must reflect the SECOND delete's state (active), not the first's
    (inactive). This is the test that fails if a cycle ever reads a stale sidecar, and it
    also proves restore does not consume the ability to delete again.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = _vendor(db_session, is_active=False)
    vendor_id = vendor.id

    # Cycle 1: deactivated at delete time -> comes back deactivated.
    _delete(client, admin, vendor_id)
    _restore(client, admin, vendor_id)
    first = _row(db_session, vendor_id)
    assert first.is_active is False
    assert first.is_active_before_delete is None

    # The shop deliberately re-approves it for use -- a separate, separately audited act.
    reactivate = client.put(
        f"{VENDORS}/{vendor_id}",
        headers=headers_for(admin),
        json={"version": 0, "is_active": True},
    )
    assert reactivate.status_code == status.HTTP_200_OK, reactivate.text
    assert _row(db_session, vendor_id).is_active is True

    # Cycle 2: active at delete time -> comes back active.
    _delete(client, admin, vendor_id)
    assert _row(db_session, vendor_id).is_active_before_delete is True
    _restore(client, admin, vendor_id)

    second = _row(db_session, vendor_id)
    assert second.is_deleted is False
    assert second.is_active is True, "cycle 2 restored cycle 1's state -- a stale sidecar was read"
    assert second.is_active_before_delete is None


def test_the_sidecar_is_not_writable_through_any_request_schema(client: TestClient, db_session: Session):
    """The sidecar must stay reachable ONLY by ``delete_vendor`` and ``restore_vendor``.

    ``update_vendor`` runs a blind ``setattr`` loop over ``model_dump(exclude_unset=True)``,
    so anything ``VendorUpdate`` declares is client-writable. Today the column is safe
    purely because it is ABSENT from every request schema -- the same protection
    ``Part.backflush_components`` gets from being kept out of ``PartBase``, and the same
    way it is lost: a future "add the missing vendor fields to the update schema" cleanup
    would silently hand the client a lever on stored supplier-approval history.

    Asserted twice: structurally (the field is in no request schema) and behaviourally
    (a request that sends it anyway changes nothing).
    """
    from app.schemas.purchasing import VendorBase, VendorCreate, VendorUpdate

    for schema in (VendorBase, VendorCreate, VendorUpdate):
        assert "is_active_before_delete" not in schema.model_fields, (
            f"{schema.__name__} exposes the delete/restore sidecar -- update_vendor's blind "
            "setattr loop would make it client-writable"
        )

    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = _vendor(db_session, is_active=True)
    vendor_id = vendor.id

    resp = client.put(
        f"{VENDORS}/{vendor_id}",
        headers=headers_for(admin),
        json={"version": 0, "name": "Sidecar Poker", "is_active_before_delete": False},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert _row(db_session, vendor_id).is_active_before_delete is None, "a PUT wrote the delete/restore sidecar"


def test_restore_audit_row_records_the_restored_is_active(client: TestClient, db_session: Session):
    """Invariant 2: the restore is a state change and must be recorded -- including the
    approval-relevant flag it writes. The one column a reader would ask about must not be
    the one column the restore row omits."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    active = _vendor(db_session, is_active=True)
    inactive = _vendor(db_session, is_active=False)
    active_id, inactive_id = active.id, inactive.id

    for vendor_id in (active_id, inactive_id):
        _delete(client, admin, vendor_id)
        _restore(client, admin, vendor_id)

    active_rows = _restore_audit_rows(db_session, active_id)
    assert len(active_rows) == 1
    assert active_rows[0].new_values["is_deleted"] is False
    assert active_rows[0].new_values["is_active"] is True
    # is_active genuinely moved (the delete forced it False), so it is in the diff.
    assert "is_active" in active_rows[0].extra_data["changes"]

    inactive_rows = _restore_audit_rows(db_session, inactive_id)
    assert len(inactive_rows) == 1
    assert inactive_rows[0].new_values["is_deleted"] is False
    assert inactive_rows[0].new_values["is_active"] is False
    # Honest diff: False -> False is not a change, so only is_deleted is listed. The
    # restored value is still recorded on new_values above.
    assert "is_active" not in inactive_rows[0].extra_data["changes"]
    assert "is_deleted" in inactive_rows[0].extra_data["changes"]


def test_restore_audit_row_snapshots_the_prior_is_active_it_does_not_assume_it(client: TestClient, db_session: Session):
    """The audit row must READ the pre-restore ``is_active``, never assert it.

    ``delete_vendor`` always forces the flag False, so hard-coding ``old_values`` looks
    safe -- and is wrong for a row that reached ``is_deleted=True`` + ``is_active=True``
    through the pre-#231 ``PUT`` reanimation hole. That hole is closed, but the rows it
    produced were never repaired, and restoring one really does move is_active True ->
    False. Asserting False would record that move as a no-op in the tamper-evident
    audit_log (invariant 2) and drop ``is_active`` from the changes diff entirely -- the
    one column a reader would ask about.

    The state is seeded at the DB level because no endpoint can produce it any more; that
    is the point.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = _vendor(db_session, is_active=True)
    vendor_id = vendor.id

    _delete(client, admin, vendor_id)

    # Reanimate the way the pre-#231 PUT did: deleted, but still flagged active, and with
    # a NULL sidecar like every row deleted before 082.
    row = _row(db_session, vendor_id)
    row.is_active = True
    row.is_active_before_delete = None
    db_session.commit()

    _restore(client, admin, vendor_id)

    restored = _row(db_session, vendor_id)
    assert restored.is_deleted is False
    # Behaviour is unchanged and still errs OFF -- only the record of it is under test.
    assert restored.is_active is False

    rows = _restore_audit_rows(db_session, vendor_id)
    assert len(rows) == 1
    assert rows[0].old_values["is_active"] is True, (
        "the restore audit row asserted a prior is_active of False for a row that was "
        "actually flagged active -- old_values is hard-coded instead of snapshotted"
    )
    assert rows[0].new_values["is_active"] is False
    # A real True -> False move on an approved-supplier flag must appear in the diff.
    assert "is_active" in rows[0].extra_data["changes"]


# ===========================================================================
# §8. Tenant isolation on the new view (invariant 1)
# ===========================================================================


def test_deleted_view_is_tenant_scoped_both_ways(client: TestClient, db_session: Session):
    """A new view over previously-invisible rows is exactly where a tenancy leak would
    hide, so both directions are asserted."""
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    admin2 = make_user(db_session, role=UserRole.ADMIN, company_id=2)
    v1 = _vendor(db_session, company_id=1)
    v2 = _vendor(db_session, company_id=2)
    code1, code2 = v1.code, v2.code

    _delete(client, admin1, v1.id)
    _delete(client, admin2, v2.id)

    view1 = _codes(_list(client, admin1, deleted_only=True))
    view2 = _codes(_list(client, admin2, deleted_only=True))

    assert code1 in view1 and code2 not in view1
    assert code2 in view2 and code1 not in view2


def test_restore_of_a_cross_company_vendor_is_404_and_changes_nothing(client: TestClient, db_session: Session):
    """Company 1 can neither see nor restore company 2's deleted vendor."""
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    admin2 = make_user(db_session, role=UserRole.ADMIN, company_id=2)
    other = _vendor(db_session, company_id=2, is_active=True)
    other_id = other.id
    _delete(client, admin2, other_id)

    resp = client.post(f"{VENDORS}/{other_id}/restore", headers=headers_for(admin1))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    # Untouched: still deleted, sidecar intact, still restorable by its own tenant.
    row = _row(db_session, other_id)
    assert row.is_deleted is True
    assert row.is_active_before_delete is True
    _restore(client, admin2, other_id)
    assert _row(db_session, other_id).is_active is True


# ===========================================================================
# §9. Guards from the vendor-read tightening this PR must not break
#
# The read tightening is what made this PR urgent, so its load-bearing exceptions are
# re-pinned here against THESE paths. (The exhaustive per-site sweep lives in
# test_vendor_soft_delete_sweep.py -- these are the three that this feature touches.)
# ===========================================================================


def test_restore_still_sees_a_soft_deleted_vendor(client: TestClient, db_session: Session):
    """``restore_vendor`` uses a RAW lookup, not ``_live_vendor_or_404`` -- seeing the
    tombstone is its entire job. Given the helper's filter it would otherwise 404 on
    exactly the rows the new list view now hands the UI."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = _vendor(db_session)
    vendor_id, code = vendor.id, vendor.code

    _delete(client, admin, vendor_id)
    # The UI's actual flow: discover through the restore view, act on what it returned.
    assert code in _codes(_list(client, admin, deleted_only=True))
    _restore(client, admin, vendor_id)
    assert code not in _codes(_list(client, admin, deleted_only=True))
    assert code in _codes(_list(client, admin))


def test_double_delete_is_still_400_and_lists_once(client: TestClient, db_session: Session):
    """``delete_vendor`` is also a raw lookup, so the re-delete guard still answers 400
    "already deleted" rather than 404 -- and a refused second delete must not reset the
    provenance or duplicate the row in the restore view."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = _vendor(db_session)
    vendor_id, code = vendor.id, vendor.code

    _delete(client, admin, vendor_id)
    before = _row(db_session, vendor_id).deleted_at

    second = client.delete(f"{VENDORS}/{vendor_id}", headers=headers_for(admin))
    assert second.status_code == status.HTTP_400_BAD_REQUEST, second.text
    assert "already deleted" in second.json()["detail"]

    row = _row(db_session, vendor_id)
    assert row.deleted_at == before
    assert row.is_active_before_delete is True, "the refused re-delete overwrote the sidecar with the masked value"
    assert _codes(_list(client, admin, deleted_only=True)).count(code) == 1


def test_a_deleted_vendors_code_is_still_taken(client: TestClient, db_session: Session):
    """The duplicate probes DELIBERATELY count soft-deleted rows: ``uq_vendors_company_code``
    has no partial predicate, so a deleted vendor still OWNS its code. That is what lets
    ``restore_vendor`` skip a collision check it could not implement anyway (it has no way
    to renumber) -- a precondition of the restore verb, not a side note. This PR is what
    makes it reachable: the restore view hands a deleted vendor's identity to any
    authenticated reader in the tenant, so "someone squats the code while it is away" stops
    being theoretical.

    THE CANARY IS LOAD-BEARING. The status code alone cannot tell the probe from the
    ``except IntegrityError`` backstop underneath it: filter the probe and the insert still
    trips the unique constraint, and the handler surfaces the very same 400 (verified by
    mutation -- a 400-plus-restore assertion alone passes against a filtered probe). What
    differs is the ``db.rollback()`` on the backstop path, and the test session IS the
    request session, so an uncommitted row flushed here survives the probe path and is
    destroyed by the backstop path. The exhaustive per-site version of this argument lives
    in ``test_vendor_soft_delete_sweep.py`` §9.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = _vendor(db_session)
    vendor_id, code = vendor.id, vendor.code

    _delete(client, admin, vendor_id)

    canary = Vendor(code=f"CANARY{_next():04d}", name="Rollback canary", company_id=1)
    db_session.add(canary)
    db_session.flush()  # uncommitted: only a rollback destroys it
    canary_id = canary.id

    taken = client.post(VENDORS, headers=headers_for(admin), json={"code": code, "name": "Code Squatter"})
    assert taken.status_code == status.HTTP_400_BAD_REQUEST, taken.text
    assert taken.json()["detail"] == "Vendor code already exists"
    assert db_session.query(Vendor).filter(Vendor.id == canary_id).first() is not None, (
        "the 400 must come from the pre-insert probe, which refuses before touching the "
        "session -- a surviving canary proves no IntegrityError rollback happened"
    )

    # ...which is precisely why the restore below cannot violate the unique constraint.
    _restore(client, admin, vendor_id)
    assert _row(db_session, vendor_id).code == code


# ===========================================================================
# §10. Role posture: reading the restore view is not gated; restoring is
# ===========================================================================


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER, UserRole.SUPERVISOR])
def test_deleted_view_readable_below_the_restore_gate_but_not_actionable(
    client: TestClient, db_session: Session, role: UserRole
):
    """Deliberate posture, mirroring the PO twin: the LIST endpoint stays on
    ``get_current_user`` because ``deleted_only`` discloses rows the same reader could
    already see before the delete. The privileged act is the restore verb."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    reader = make_user(db_session, role=role, company_id=1)
    vendor = _vendor(db_session)
    vendor_id, code = vendor.id, vendor.code
    _delete(client, admin, vendor_id)

    assert code in _codes(_list(client, reader, deleted_only=True))
    resp = client.post(f"{VENDORS}/{vendor_id}/restore", headers=headers_for(reader))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    # Refused means refused: still deleted.
    assert _row(db_session, vendor_id).is_deleted is True


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER])
def test_restore_allowed_inside_the_gate(client: TestClient, db_session: Session, role: UserRole):
    """The positive half of the RBAC pair -- both roles inside ``[ADMIN, MANAGER]``."""
    actor = make_user(db_session, role=role, company_id=1)
    vendor = _vendor(db_session)
    vendor_id, code = vendor.id, vendor.code

    _delete(client, actor, vendor_id)
    _restore(client, actor, vendor_id)

    assert code not in _codes(_list(client, actor, deleted_only=True))
    assert _row(db_session, vendor_id).is_deleted is False


# ===========================================================================
# §11. The end-to-end point of this PR
# ===========================================================================


def test_deleted_vendor_blocks_po_creation_until_it_is_restored(client: TestClient, db_session: Session):
    """The round trip this whole PR exists to make possible.

    ``create_purchase_order`` refuses a soft-deleted vendor with 404 "Vendor not found" --
    one of the five write-path refusals the read tightening introduced. Before this PR
    that refusal was terminal from the UI: nothing could list the deleted vendor, so
    nothing could restore it. Now: delete -> refusal -> DISCOVER through the restore view
    -> restore -> the same call succeeds.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    part_id = make_po_line(db_session, company_id=1, quantity_ordered=10).part_id
    vendor = _vendor(db_session, is_active=True)
    vendor_id, code = vendor.id, vendor.code

    # Positive control first: the call works while the vendor is live.
    ok = client.post(PO_BASE, headers=headers_for(admin), json=_po_body(vendor_id, part_id))
    assert ok.status_code == status.HTTP_200_OK, ok.text
    # ...and that PO must be closed out, or the active-PO guardrail refuses the delete.
    db_session.query(PurchaseOrder).filter(PurchaseOrder.id == ok.json()["id"]).one().status = POStatus.CLOSED
    db_session.commit()

    # 1. Delete.
    _delete(client, admin, vendor_id)

    # 2. The refusal.
    refused = client.post(PO_BASE, headers=headers_for(admin), json=_po_body(vendor_id, part_id))
    assert refused.status_code == status.HTTP_404_NOT_FOUND, refused.text
    assert refused.json()["detail"] == "Vendor not found"

    # 3. Discovery -- the step that did not exist before this PR.
    assert code in _codes(_list(client, admin, deleted_only=True))

    # 4. Restore.
    _restore(client, admin, vendor_id)

    # 5. The previously-refused operation now succeeds.
    allowed = client.post(PO_BASE, headers=headers_for(admin), json=_po_body(vendor_id, part_id))
    assert allowed.status_code == status.HTTP_200_OK, allowed.text


def test_restoring_a_deactivated_vendor_does_not_re_enable_po_creation(client: TestClient, db_session: Session):
    """The security counterpart of the round trip above, and the reason the owner's
    decision matters in practice.

    ``create_purchase_order`` refuses an INACTIVE vendor with the same 404. If restore
    unconditionally reactivated, undoing a delete would silently hand back the ability to
    raise purchase orders against a supplier the shop had switched off -- no approval, no
    audit of an approval, nothing. Here the refusal survives the restore, and only a
    deliberate ``PUT`` lifts it.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    part_id = make_po_line(db_session, company_id=1, quantity_ordered=10).part_id
    vendor = _vendor(db_session, is_active=False)
    vendor_id = vendor.id

    _delete(client, admin, vendor_id)
    _restore(client, admin, vendor_id)

    still_refused = client.post(PO_BASE, headers=headers_for(admin), json=_po_body(vendor_id, part_id))
    assert still_refused.status_code == status.HTTP_404_NOT_FOUND, still_refused.text
    assert still_refused.json()["detail"] == "Vendor not found"

    # The deliberate, separately audited re-activation is what lifts it.
    reactivate = client.put(
        f"{VENDORS}/{vendor_id}",
        headers=headers_for(admin),
        json={"version": 0, "is_active": True},
    )
    assert reactivate.status_code == status.HTTP_200_OK, reactivate.text

    allowed = client.post(PO_BASE, headers=headers_for(admin), json=_po_body(vendor_id, part_id))
    assert allowed.status_code == status.HTTP_200_OK, allowed.text
