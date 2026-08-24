"""Taking a part out of use without deleting it, and putting it back.

WHY THESE TWO VERBS EXIST
-------------------------
The materials recut left empty leftover SKUs behind — a sheet size the shop no
longer buys, sitting at 0 on hand — and until now nothing could retire one.
``PartUpdate`` carries neither ``is_active`` nor ``status``, and the only writer
of those columns was ``delete_part``, which also soft-deletes. So the choice was
"leave it cluttering every picker" or "delete it and break every document naming
it". ``POST /parts/{id}/deactivate`` is the third option, and
``test_empty_leftover_sku_can_be_marked_inactive_without_deleting`` is the
acceptance record for it.

THE ONE REFUSAL THAT IS A SECURITY CONTROL
------------------------------------------
Both verbs refuse a SOFT-DELETED part with **404**. That is not tidiness.
Invariant 3 records that ``parts.is_active`` doubles as the soft-delete MASK —
``delete_part`` sets ``is_deleted`` AND ``is_active=False`` AND
``status='obsolete'`` together — so a verb able to set ``is_active=True`` on a
tombstoned row would be clearing a delete mask. That is the exact 2026-08-16
``Vendor`` trap, where six vendor query sites looked safe only because
``delete_vendor`` also cleared ``is_active``. ``POST /parts/{id}/restore`` is the
verb for a deleted part; these two are not it and must not become it by accident.
``test_deleted_part_cannot_be_activated_or_deactivated`` pins that, and its
docstring says so, so nobody relaxes it in passing.

THE SECOND HALF OF THIS FILE: WHAT RESTORE DOES WITH ``is_active`` (P1)
----------------------------------------------------------------------
Everything below the "P1" banner covers ``POST /parts/{id}/restore``, which is
here rather than in a file of its own because the two verbs above are what MADE
it a hazard. Restore used to hard-code ``is_active = True`` / ``status = "active"``
— harmless while "inactive but not deleted" was unreachable for a part, and a
silent reversal of a deliberate, reasoned, audited retirement the moment
``deactivate`` and the combine's ``deactivate_source`` started producing that
state at scale. Migration ``086``'s ``is_active_before_delete`` sidecar is the
fix, mirroring what ``082`` did for ``Vendor``; the migration itself is covered in
``tests/test_migration_086_part_active_before_delete.py``.

Two of those tests exist to refuse a "simplification" rather than to describe a
feature: ``test_delete_still_writes_the_is_active_mask`` (dropping the mask so
restore has nothing to put back is a security regression, not a shortcut) and
``test_restore_of_a_legacy_null_sidecar_resolves_restrictive`` (the NULL fallback
is a deliberate BREAK from the old behaviour, not a compatibility bug).
"""

from typing import Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.inventory import InventoryItem
from app.models.part import Part
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2

# The owner's leftover SKU: a sheet size the shop no longer buys, at 0 on hand.
LEFTOVER_NUMBER = "0.0625-48X120-304SS"

# Tokens are minted directly; this hash is never used for a login.
FIXTURE_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def _user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"activation-{n}@co{company_id}.test",
        employee_id=f"ACT-{n:05d}",
        first_name="Activation",
        last_name=f"C{company_id}",
        hashed_password=FIXTURE_PASSWORD_HASH,
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _part(
    db: Session,
    *,
    number: Optional[str] = None,
    name: str = "Sheet stock plate",
    is_active: bool = True,
    status: str = "active",
    is_deleted: bool = False,
    company_id: int = COMPANY_A,
) -> Part:
    _ensure_company(db, company_id)
    part = Part(
        part_number=number or f"PA-{_next():05d}",
        name=name,
        description=name,
        part_type="raw_material",
        unit_of_measure="each",
        is_active=is_active,
        status=status,
        is_deleted=is_deleted,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _stock(db: Session, part: Part, *, qty: float, location: str = "RACK-1", status: str = "available"):
    item = InventoryItem(
        part_id=part.id,
        location=location,
        warehouse="MAIN",
        quantity_on_hand=qty,
        quantity_allocated=0.0,
        quantity_available=qty,
        lot_number=f"LOT-{_next():05d}",
        unit_cost=1.0,
        status=status,
        is_active=True,
        company_id=part.company_id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _deactivate(
    client: TestClient,
    headers: dict,
    part_id: int,
    *,
    reason: str = "Sheet size no longer purchased",
    acknowledge_remaining_stock: bool = False,
):
    return client.post(
        f"/api/v1/parts/{part_id}/deactivate",
        json={"reason": reason, "acknowledge_remaining_stock": acknowledge_remaining_stock},
        headers=headers,
    )


def _activate(client: TestClient, headers: dict, part_id: int, *, reason: Optional[str] = None):
    body = {} if reason is None else {"reason": reason}
    return client.post(f"/api/v1/parts/{part_id}/activate", json=body, headers=headers)


def _reload(db: Session, part_id: int) -> Optional[Part]:
    db.expire_all()
    return db.query(Part).filter(Part.id == part_id).first()


def _part_audits(db: Session, part_id: int):
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == "part", AuditLog.resource_id == part_id)
        .order_by(AuditLog.id.asc())
        .all()
    )


@pytest.fixture
def admin_hdrs(db_session: Session) -> dict:
    return _headers(_user(db_session, role=UserRole.ADMIN))


# --------------------------------------------------------------------------- #
# Acceptance
# --------------------------------------------------------------------------- #


def test_empty_leftover_sku_can_be_marked_inactive_without_deleting(
    db_session: Session, client: TestClient, admin_hdrs
):
    """ACCEPTANCE 2: '0.0625-48X120-304SS' at 0 goes Inactive, and stays in the catalog.

    ``is_deleted`` must remain ``False`` and the part must still resolve by id.
    Retiring a SKU and deleting one are different decisions with different
    consequences: a delete tombstones a row that PO lines, MTRs, travelers and
    spreadsheets still name, while a deactivation only takes it out of the pickers.
    """
    part = _part(db_session, number=LEFTOVER_NUMBER, name="Sheet 16GA 304 stainless 48 x 120")

    response = _deactivate(client, admin_hdrs, part.id, reason="Sheet size no longer purchased")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["part_id"] == part.id
    assert body["part_number"] == LEFTOVER_NUMBER
    assert body["is_active"] is False
    assert body["status"] == "obsolete"
    assert body["no_op"] is False

    reloaded = _reload(db_session, part.id)
    assert reloaded is not None, "the part must still resolve by id"
    assert reloaded.is_active is False
    assert reloaded.status == "obsolete"
    assert reloaded.is_deleted is False
    assert reloaded.deleted_at is None
    assert reloaded.deleted_by is None

    # Invariant 2: the state change is on the tamper-evident trail, with the reason.
    audits = _part_audits(db_session, part.id)
    assert len(audits) == 1
    assert audits[0].action == "UPDATE"
    assert audits[0].old_values["is_active"] is True
    assert audits[0].new_values["is_active"] is False
    assert audits[0].new_values["status"] == "obsolete"
    assert audits[0].extra_data["reason"] == "Sheet size no longer purchased"
    assert audits[0].extra_data["quantity_on_hand_at_deactivation"] == 0.0
    assert "Not deleted" in audits[0].description


# --------------------------------------------------------------------------- #
# The soft-delete mask guard
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("verb", ["deactivate", "activate"])
def test_deleted_part_cannot_be_activated_or_deactivated(
    db_session: Session, client: TestClient, admin_hdrs, verb: str
):
    """A soft-deleted part is 404 on BOTH verbs — "restore it first". DO NOT RELAX THIS.

    This refusal is a security control, not tidiness. Invariant 3 records that
    ``parts.is_active`` doubles as the SOFT-DELETE MASK: ``delete_part`` sets
    ``is_deleted`` AND ``is_active=False`` AND ``status='obsolete'`` together. A
    verb able to write ``is_active=True`` on a tombstoned row would therefore clear
    a delete mask — the exact 2026-08-16 ``Vendor`` trap, where six vendor query
    sites looked safe only because ``delete_vendor`` also cleared ``is_active``.
    ``POST /parts/{id}/restore`` is the verb for a deleted part; these two are not
    it, and they must not become it by loosening this test.
    """
    part = _part(db_session, is_active=False, status="obsolete", is_deleted=True)

    if verb == "deactivate":
        response = _deactivate(client, admin_hdrs, part.id)
    else:
        response = _activate(client, admin_hdrs, part.id)

    assert response.status_code == 404, response.text
    assert "restore it first" in response.json()["detail"]

    reloaded = _reload(db_session, part.id)
    assert reloaded.is_deleted is True
    assert reloaded.is_active is False
    assert reloaded.status == "obsolete"
    assert _part_audits(db_session, part.id) == []


# --------------------------------------------------------------------------- #
# Deactivate
# --------------------------------------------------------------------------- #


def test_deactivate_with_stock_on_hand_requires_acknowledgement(db_session: Session, client: TestClient, admin_hdrs):
    """Deactivating a part with stock is legitimate, but never accidental.

    The refusal names the quantity, and it is raised BEFORE the first ``setattr``,
    so a refused request leaves the row byte-identical.
    """
    part = _part(db_session)
    _stock(db_session, part, qty=7.0)

    refused = _deactivate(client, admin_hdrs, part.id)
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert "still has 7 on hand" in detail

    reloaded = _reload(db_session, part.id)
    assert reloaded.is_active is True
    assert reloaded.status == "active"
    assert _part_audits(db_session, part.id) == []

    accepted = _deactivate(client, admin_hdrs, part.id, acknowledge_remaining_stock=True)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["is_active"] is False

    audits = _part_audits(db_session, part.id)
    assert len(audits) == 1
    assert audits[0].extra_data["acknowledged_remaining_stock"] is True
    assert audits[0].extra_data["quantity_on_hand_at_deactivation"] == 7.0


def test_deactivate_counts_held_stock_as_stock(db_session: Session, client: TestClient, admin_hdrs):
    """Held and quarantined material is still material on a shelf.

    ``part_total_on_hand`` counts EVERY stock row — the same definition the
    combine verb's ``source_still_has_stock`` refusal uses, imported rather than
    re-derived. Hiding the part that names held stock from every list in the app is
    exactly what makes this worth confirming.
    """
    part = _part(db_session)
    _stock(db_session, part, qty=3.0, status="on_hold")

    refused = _deactivate(client, admin_hdrs, part.id)
    assert refused.status_code == 409, refused.text
    assert "still has 3 on hand" in refused.json()["detail"]


@pytest.mark.parametrize("reason", ["", "   "])
def test_deactivate_requires_a_reason(db_session: Session, client: TestClient, admin_hdrs, reason: str):
    """``reason`` is REQUIRED, and whitespace is not a reason.

    Deactivation removes the part from every picker, search and purchasing signal,
    so "why" is the only thing that makes the change reviewable later — the same
    rule receiving void, NCR void, vendor delete and part renumber all follow.
    """
    part = _part(db_session)

    response = _deactivate(client, admin_hdrs, part.id, reason=reason)
    assert response.status_code == 422, response.text

    missing = client.post(f"/api/v1/parts/{part.id}/deactivate", json={}, headers=admin_hdrs)
    assert missing.status_code == 422, missing.text

    reloaded = _reload(db_session, part.id)
    assert reloaded.is_active is True
    assert _part_audits(db_session, part.id) == []


def test_redeactivating_an_inactive_part_is_a_no_op_with_no_audit_row(
    db_session: Session, client: TestClient, admin_hdrs
):
    """A request that changes nothing must not fail, and must not fabricate a change.

    A double-click and a retry are both ordinary. ``AuditService.log_update``
    returns ``None`` when there are no changes, and this verb short-circuits before
    it anyway — either way the trail must not gain a row claiming a state change
    that did not happen.
    """
    part = _part(db_session, is_active=False, status="obsolete")

    response = _deactivate(client, admin_hdrs, part.id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["no_op"] is True
    assert body["is_active"] is False
    assert body["status"] == "obsolete"

    assert _part_audits(db_session, part.id) == []
    reloaded = _reload(db_session, part.id)
    assert reloaded.is_active is False
    assert reloaded.is_deleted is False


# --------------------------------------------------------------------------- #
# Activate
# --------------------------------------------------------------------------- #


def test_activate_restores_active_and_clears_obsolete(db_session: Session, client: TestClient, admin_hdrs):
    """Activate undoes exactly what deactivate did, and audits it."""
    part = _part(db_session, is_active=False, status="obsolete")

    response = _activate(client, admin_hdrs, part.id, reason="Customer reordered this size")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_active"] is True
    assert body["status"] == "active"
    assert body["no_op"] is False

    reloaded = _reload(db_session, part.id)
    assert reloaded.is_active is True
    assert reloaded.status == "active"
    assert reloaded.is_deleted is False

    audits = _part_audits(db_session, part.id)
    assert len(audits) == 1
    assert audits[0].new_values["is_active"] is True
    assert audits[0].new_values["status"] == "active"
    assert audits[0].extra_data["reason"] == "Customer reordered this size"


def test_activate_round_trips_with_deactivate(db_session: Session, client: TestClient, admin_hdrs):
    """Deactivate then activate returns the part to use, never touching ``is_deleted``."""
    part = _part(db_session, number=LEFTOVER_NUMBER)

    assert _deactivate(client, admin_hdrs, part.id).status_code == 200
    assert _reload(db_session, part.id).is_active is False

    assert _activate(client, admin_hdrs, part.id).status_code == 200
    reloaded = _reload(db_session, part.id)
    assert reloaded.is_active is True
    assert reloaded.status == "active"
    assert reloaded.is_deleted is False

    assert len(_part_audits(db_session, part.id)) == 2


def test_activate_does_not_clobber_a_meaningful_third_status(db_session: Session, client: TestClient, admin_hdrs):
    """``status`` is rewritten only when it currently reads ``obsolete``.

    The pairing rule this verb upholds is "inactive implies obsolete", NOT "active
    implies active" — so a part parked at ``pending_approval`` keeps that status
    when it is switched back on.
    """
    part = _part(db_session, is_active=False, status="pending_approval")

    response = _activate(client, admin_hdrs, part.id)
    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is True
    assert response.json()["status"] == "pending_approval"

    reloaded = _reload(db_session, part.id)
    assert reloaded.is_active is True
    assert reloaded.status == "pending_approval"


def test_activating_an_active_part_is_a_no_op_with_no_audit_row(db_session: Session, client: TestClient, admin_hdrs):
    part = _part(db_session, is_active=True, status="active")

    response = _activate(client, admin_hdrs, part.id)
    assert response.status_code == 200, response.text
    assert response.json()["no_op"] is True
    assert _part_audits(db_session, part.id) == []


# --------------------------------------------------------------------------- #
# Roles and tenancy
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", [UserRole.SUPERVISOR, UserRole.OPERATOR, UserRole.QUALITY, UserRole.VIEWER])
@pytest.mark.parametrize("verb", ["deactivate", "activate"])
def test_activation_verbs_are_admin_manager_only(db_session: Session, client: TestClient, role: UserRole, verb: str):
    """ADMIN / MANAGER, matching the combine verb and the renumber/revision tier.

    Not the Supervisor edit tier: taking a part out of use changes what the whole
    shop can select, which is an identity-level decision rather than a form save.
    """
    part = _part(db_session, is_active=(verb == "deactivate"), status="active")
    headers = _headers(_user(db_session, role=role))

    if verb == "deactivate":
        response = _deactivate(client, headers, part.id)
    else:
        response = _activate(client, headers, part.id)

    assert response.status_code == 403, response.text
    reloaded = _reload(db_session, part.id)
    assert reloaded.is_active is (verb == "deactivate")
    assert _part_audits(db_session, part.id) == []


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER])
def test_activation_verbs_allowed_for_admin_and_manager(db_session: Session, client: TestClient, role: UserRole):
    part = _part(db_session)
    headers = _headers(_user(db_session, role=role))

    assert _deactivate(client, headers, part.id).status_code == 200
    assert _reload(db_session, part.id).is_active is False
    assert _activate(client, headers, part.id).status_code == 200
    assert _reload(db_session, part.id).is_active is True


@pytest.mark.parametrize("verb", ["deactivate", "activate"])
def test_activation_verbs_are_tenant_scoped(db_session: Session, client: TestClient, admin_hdrs, verb: str):
    """Invariant 1: another company's part is a 404, and is left byte-identical."""
    foreign = _part(db_session, company_id=COMPANY_B, is_active=(verb == "deactivate"))

    if verb == "deactivate":
        response = _deactivate(client, admin_hdrs, foreign.id)
    else:
        response = _activate(client, admin_hdrs, foreign.id)

    assert response.status_code == 404, response.text
    reloaded = _reload(db_session, foreign.id)
    assert reloaded.company_id == COMPANY_B
    assert reloaded.is_active is (verb == "deactivate")
    assert _part_audits(db_session, foreign.id) == []


def test_activation_verbs_404_on_an_unknown_part(db_session: Session, client: TestClient, admin_hdrs):
    assert _deactivate(client, admin_hdrs, 999_999).status_code == 404
    assert _activate(client, admin_hdrs, 999_999).status_code == 404


# =========================================================================== #
# P1 — a restore returns the RECORD, not the PERMISSION
#
# ``POST /parts/{id}/restore`` used to do ``part.restore(); part.is_active = True;
# part.status = "active"``. That was harmless while "inactive but not deleted" was
# essentially unreachable for a part — ``PartUpdate`` carries neither column and
# the only writer was ``delete_part``, which also sets ``is_deleted``. The two
# verbs at the top of this file, and the combine's ``deactivate_source`` flag,
# DELIBERATELY CREATE THAT STATE AT SCALE, so the hazard became ours:
#
#     Fold ``.0625-60X144-304SS`` away and retire it (audited, with a reason).
#     Someone later deletes the empty husk; someone else restores it. Pre-086 it
#     came back ACTIVE and ``status='active'`` — back in every picker, selectable
#     again for receiving and BOM lines — the deliberate retirement silently
#     reversed, with NO audit row saying anybody decided to re-activate it.
#
# The fix is the ``is_active_before_delete`` sidecar (migration 086), mirroring
# what 082 did for ``Vendor``: written by the delete, read and CLEARED by the
# restore, and NULL — "we never recorded one" — resolves to the RESTRICTIVE value.
# Invariant 3 names ``is_active`` as a delete MASK rather than a filter, and says
# a restore returns the record, not the permission.
#
# THE ASYMMETRY IS THE ARGUMENT, and it is why none of these tests may be relaxed
# into "restore reactivates": restoring too restrictively costs one explicit,
# audited re-activation and is visible immediately (the part is missing from a
# list somebody expected it in); restoring too permissively is indistinguishable
# from a legitimate approval and so is never detected at all.
# =========================================================================== #


def _delete(client: TestClient, headers: dict, part_id: int):
    return client.delete(f"/api/v1/parts/{part_id}", headers=headers)


def _restore(client: TestClient, headers: dict, part_id: int):
    return client.post(f"/api/v1/parts/{part_id}/restore", headers=headers)


def test_restore_returns_a_deactivated_part_switched_off(db_session: Session, client: TestClient, admin_hdrs):
    """THE HEADLINE CASE: retire → delete → restore comes back INACTIVE.

    This is the whole point of migration 086. The part was deliberately taken out
    of use, with a reason, on the audit trail. Undoing a DELETE is a records
    decision; putting a retired part number back on every picker is an engineering
    decision, and it must not happen as a side effect of the first one.

    ``status`` follows ``is_active`` and is never hard-coded, so the ``obsolete``
    the delete wrote survives — upholding the same "inactive implies obsolete"
    pairing the deactivate/activate verbs enforce.
    """
    part = _part(db_session, number=LEFTOVER_NUMBER, name="Sheet 16GA 304 stainless 48 x 120")

    # 1. Retire it, exactly as the combine's deactivate_source does.
    assert _deactivate(client, admin_hdrs, part.id, reason="Folded onto SH-A240-304").status_code == 200
    reloaded = _reload(db_session, part.id)
    assert reloaded.is_active is False and reloaded.status == "obsolete"

    # 2. Somebody deletes the empty husk. The mask is written, and the sidecar
    #    remembers the value it is masking.
    assert _delete(client, admin_hdrs, part.id).status_code == 200
    deleted = _reload(db_session, part.id)
    assert deleted.is_deleted is True
    assert deleted.is_active is False
    assert deleted.is_active_before_delete is False, "the delete must remember the PRE-delete value"

    # 3. Somebody else restores it. The record comes back; the permission does not.
    response = _restore(client, admin_hdrs, part.id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_active"] is False
    assert body["status"] == "obsolete"
    # The response DISCLOSES the restrictive outcome rather than leaving the caller
    # to discover it when the part is missing from a picker.
    assert "inactive" in body["message"].lower()

    restored = _reload(db_session, part.id)
    assert restored.is_deleted is False
    assert restored.deleted_at is None
    assert restored.is_active is False, "a deliberate retirement was silently reversed — see migration 086"
    assert restored.status == "obsolete"
    # Cleared, so a later cycle can never read this one's value.
    assert restored.is_active_before_delete is None


def test_restore_returns_an_active_part_active(db_session: Session, client: TestClient, admin_hdrs):
    """The ordinary case still works: an ACTIVE part deleted by mistake comes back on.

    The restrictive resolution applies to the UNKNOWN, not to everything. A part
    that was in use when it was deleted is restored in use, and its ``obsolete``
    status — written by the delete, not by anybody's decision — is lifted back to
    ``active`` with it. Without this test the safe reading of 086 would be "restore
    always switches things off", which would be a different bug.
    """
    part = _part(db_session)
    assert part.is_active is True and part.status == "active"

    assert _delete(client, admin_hdrs, part.id).status_code == 200
    deleted = _reload(db_session, part.id)
    assert deleted.is_active_before_delete is True
    # The MASK is still written — that is deliberate, not an oversight. See below.
    assert deleted.is_active is False
    assert deleted.status == "obsolete"

    response = _restore(client, admin_hdrs, part.id)
    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is True
    assert response.json()["status"] == "active"

    restored = _reload(db_session, part.id)
    assert restored.is_active is True
    assert restored.status == "active"
    assert restored.is_deleted is False
    assert restored.is_active_before_delete is None


def test_delete_still_writes_the_is_active_mask(db_session: Session, client: TestClient, admin_hdrs):
    """The tempting shortcut — stop masking, so restore has nothing to put back.

    It is a SECURITY REGRESSION, and this test is here to refuse it. ``GET /parts/``
    and ``GET /materials/`` both default ``active_only=True`` and filter this flag,
    as does every picker built on them, so ``is_active = False`` is a deliberate
    SECOND layer behind the ``is_deleted`` filters. Dropping it would silently
    change what "deleted" means to any query added later — the 2026-08-16 ``Vendor``
    trap, where six query sites looked safe only because the delete also cleared
    ``is_active``.

    Invariant 3 is explicit that the mask is a mask and not a filter, which is
    exactly why the sidecar exists instead.
    """
    part = _part(db_session)

    assert _delete(client, admin_hdrs, part.id).status_code == 200

    deleted = _reload(db_session, part.id)
    assert deleted.is_deleted is True
    assert deleted.is_active is False, "the delete mask was dropped — see invariant 3"
    assert deleted.status == "obsolete"


def test_restore_of_a_legacy_null_sidecar_resolves_restrictive(db_session: Session, client: TestClient, admin_hdrs):
    """NULL means "we never recorded one", and that unknown resolves to OFF.

    NULL is reachable for two real reasons and both are permanent: a part deleted
    BEFORE 086 shipped, and a part deleted through ``DELETE /materials/{id}``, a
    SECOND soft-delete writer of ``parts.is_active`` that does not record the
    sidecar. The prior value in both cases is genuinely unknowable — the delete
    overwrote it in place, and the ``audit_log`` delete row records the deletion,
    not the flag.

    Resolving that unknown to ON would fabricate catalog state. Resolving it to OFF
    asks a human to make the call. This is a DELIBERATE BREAK from the pre-086
    unconditional ``is_active = True``; do not "preserve backward compatibility" by
    flipping it back.
    """
    part = _part(db_session, is_active=True, status="active", is_deleted=True)
    # Exactly the pre-086 shape: tombstoned, masked, and no sidecar value.
    db_session.query(Part).filter(Part.id == part.id).update(
        {"is_active": False, "status": "obsolete", "is_active_before_delete": None}
    )
    db_session.commit()
    assert _reload(db_session, part.id).is_active_before_delete is None

    response = _restore(client, admin_hdrs, part.id)
    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is False

    restored = _reload(db_session, part.id)
    assert restored.is_deleted is False
    assert restored.is_active is False
    assert restored.status == "obsolete"


def test_restore_audit_row_records_the_real_prior_values(db_session: Session, client: TestClient, admin_hdrs):
    """The restore's audit row is READ off the row, never asserted.

    THE BUG: it hard-coded ``old_values={"is_deleted": True, "status": "obsolete"}``
    and ``new_values={"is_deleted": False, "status": "active"}``. Both halves could
    be false at once, and the FABRICATED prior value went onto the tamper-evident
    chain (invariant 2) where nothing downstream can tell it from a measured one.

    ``pending_approval`` is the fixture on purpose: it is a real third status that
    the old hard-coded ``"obsolete"`` would have misreported outright. ``is_active``
    is in the diff on purpose too — it is the approval-relevant flag this verb
    writes, and leaving it out would make the one column a reader would ask about
    the one column the restore row does not record.
    """
    part = _part(db_session, is_active=False, status="pending_approval", is_deleted=True)
    db_session.query(Part).filter(Part.id == part.id).update({"is_active_before_delete": False})
    db_session.commit()

    assert _restore(client, admin_hdrs, part.id).status_code == 200

    audits = _part_audits(db_session, part.id)
    assert len(audits) == 1
    row = audits[0]
    assert row.action == "RESTORE"
    # The REAL prior state, including the status the old code would have claimed
    # was "obsolete".
    assert row.old_values == {"is_deleted": True, "is_active": False, "status": "pending_approval"}
    assert row.new_values == {"is_deleted": False, "is_active": False, "status": "pending_approval"}
    # …and the description says the part came back switched off, so the row reads
    # correctly to a human without re-deriving the COALESCE.
    assert "INACTIVE" in row.description

    restored = _reload(db_session, part.id)
    # The minimal-status rewrite rule: only ``obsolete`` is ever lifted, and only
    # when the part resolves active. A meaningful third value is never clobbered.
    assert restored.status == "pending_approval"
    assert restored.is_active is False


def test_a_second_delete_restore_cycle_reads_its_own_remembered_value(
    db_session: Session, client: TestClient, admin_hdrs
):
    """The sidecar is cleared on restore, so cycle two cannot read cycle one's value.

    Left set, a part deleted-and-restored twice would resolve against a stale
    remembered flag — and it would do so silently, since nothing else reads the
    column. The two cycles below deliberately have OPPOSITE answers, so a stale
    read produces the wrong one rather than the same one twice.
    """
    part = _part(db_session)

    # Cycle 1: active when deleted -> comes back active.
    assert _delete(client, admin_hdrs, part.id).status_code == 200
    assert _restore(client, admin_hdrs, part.id).status_code == 200
    assert _reload(db_session, part.id).is_active is True
    assert _reload(db_session, part.id).is_active_before_delete is None

    # Now retire it deliberately, then run cycle 2.
    assert _deactivate(client, admin_hdrs, part.id, reason="Superseded by a new number").status_code == 200
    assert _delete(client, admin_hdrs, part.id).status_code == 200
    assert _reload(db_session, part.id).is_active_before_delete is False

    assert _restore(client, admin_hdrs, part.id).status_code == 200
    restored = _reload(db_session, part.id)
    assert restored.is_active is False, "cycle 2 read cycle 1's remembered value"
    assert restored.is_active_before_delete is None


def test_a_restored_inactive_part_is_reactivated_by_the_audited_activate_verb(
    db_session: Session, client: TestClient, admin_hdrs
):
    """A restrictive restore is only defensible if the undo actually exists.

    The whole asymmetry argument ("restoring too restrictively costs one explicit,
    audited re-activation") depends on that re-activation being performable. It is —
    ``POST /parts/{id}/activate``, which writes its own audit row naming whoever
    made the call, which is precisely the record the old unconditional re-activate
    never produced.

    KNOWN GAP, reported rather than fixed here: a restored-inactive part is filtered
    out of ``GET /parts/`` and ``GET /materials/`` (both default ``active_only=True``,
    and neither the Parts nor the Materials page overrides it), so the
    ``PartActivationDialog`` on those pages cannot currently REACH it. The endpoint
    works; the screen that undoes a restrictive restore does not exist yet. The
    Vendors page's three-way Active / Inactive / Deleted switch is the pattern.
    """
    part = _part(db_session)
    assert _deactivate(client, admin_hdrs, part.id, reason="Folded onto the new number").status_code == 200
    assert _delete(client, admin_hdrs, part.id).status_code == 200
    assert _restore(client, admin_hdrs, part.id).status_code == 200
    assert _reload(db_session, part.id).is_active is False

    response = _activate(client, admin_hdrs, part.id, reason="Customer reordered this size")
    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is True
    assert response.json()["status"] == "active"

    reactivated = _reload(db_session, part.id)
    assert reactivated.is_active is True
    assert reactivated.status == "active"

    # The row the old behaviour never wrote: somebody DECIDED to put this number
    # back into use, and said why.
    activations = [row for row in _part_audits(db_session, part.id) if (row.extra_data or {}).get("reason")]
    assert any(row.extra_data["reason"] == "Customer reordered this size" for row in activations)


def test_no_request_schema_can_pre_seed_the_restore_sidecar(db_session: Session, client: TestClient, admin_hdrs):
    """``is_active_before_delete`` is out of every request contract, deliberately.

    ``PUT /parts/{id}`` and ``PUT /materials/{id}`` are blind ``setattr`` loops over
    whatever their schema accepted. If the sidecar were reachable from either, a
    caller could pre-load the value the NEXT restore is going to read back — turning
    the control into a request field, which is the opposite of a control.

    Asserted against the SCHEMAS rather than by probing one endpoint, because that
    is the property: no create/update contract carries the column at all.
    """
    from app.schemas.part import PartBase, PartCreate, PartUpdate

    for schema in (PartBase, PartCreate, PartUpdate):
        assert "is_active_before_delete" not in schema.model_fields, f"{schema.__name__} can seed the sidecar"

    # And the blind PUT genuinely ignores it when a caller sends it anyway.
    part = _part(db_session)
    response = client.put(
        f"/api/v1/parts/{part.id}",
        json={"name": "Renamed sheet", "is_active_before_delete": True},
        headers=admin_hdrs,
    )
    # Either status is SAFE and the pair is deliberate: today ``PartUpdate`` has no
    # ``extra="forbid"``, so Pydantic drops the unknown key and the PUT succeeds
    # (200); tightening the schema later would make it a 422. The load-bearing
    # assertion is the one below — whichever way the request is answered, the column
    # must be untouched.
    assert response.status_code in (200, 422), response.text
    assert _reload(db_session, part.id).is_active_before_delete is None
