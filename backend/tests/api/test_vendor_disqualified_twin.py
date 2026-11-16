"""Lead decision L1 (Claude-plugin brief build, 2026-11-15): a supplier Disqualified on the
Approved Supplier List must not be reachable by minting a same-name twin.

A new vendor row carries no ASL status, so a twin of a Disqualified supplier would take the
purchase orders the Disqualified record refuses (409). Every door that CREATES a vendor
therefore refuses a name that normalizes -- by the ASL import's own rule,
``supplier_approval_service.vendor_name_key`` -- to a LIVE vendor's that is Disqualified:

* ``POST /purchasing/vendors`` -- 409, nothing written;
* ``POST /po-upload/create-from-upload`` with ``create_vendor`` -- 409, no vendor and no PO;
* ``POST /purchasing/vendors/import-csv`` (and the Import Center vendor batch, which runs it)
  -- that row is an error carrying the same text, no vendor for it, the other rows unaffected.

Every refusal is paired with a positive control so a door that refused everything would fail.
Hank and the AI applier never create vendors, and the open-PO loader requires an existing one,
so there is no other door to cover.
"""

import io
from uuid import uuid4

import pytest
from fastapi import status
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.purchasing import PurchaseOrder, Vendor
from app.models.user import UserRole
from app.services import supplier_approval_service as asl
from app.services import vendor_asl_import_service
from tests.api.test_receiving_compliance import _next, headers_for, make_user
from tests.api.test_vendor_asl_approval import VENDORS, make_part, make_vendor

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

TWIN_TEXT = (
    "{} is Disqualified on the Approved Supplier List; a new vendor record cannot be created under that name. "
    "Re-evaluate the existing vendor instead."
)
# Stored with an en dash and a double space; every spelling below normalizes to it.
DISQUALIFIED_NAME = "Acme Plating – East  Works"
TWIN_SPELLINGS = ("acme plating-east works", "  ACME PLATING - EAST WORKS ", "Acme Plating—East Works")


def _vendors_named(db: Session, fragment: str):
    db.expire_all()
    return db.query(Vendor).filter(Vendor.name.ilike(f"%{fragment}%")).order_by(Vendor.id).all()


def _vendor_create_audit_rows(db: Session):
    db.expire_all()
    return db.query(AuditLog).filter(AuditLog.resource_type == "vendor", AuditLog.action == "CREATE").count()


@pytest.fixture
def disqualified(db_session: Session) -> Vendor:
    return make_vendor(db_session, name=DISQUALIFIED_NAME, approval_status="disqualified")


@pytest.fixture
def manager_headers(db_session: Session) -> dict:
    return headers_for(make_user(db_session, role=UserRole.MANAGER, company_id=1))


# =========================================================================== the shared rule


def test_the_twin_rule_is_the_asl_imports_own_name_key():
    """One normalization: the name the ASL import would bind to a vendor is the name no door may
    create again."""
    assert vendor_asl_import_service.vendor_name_key is asl.vendor_name_key
    key = asl.vendor_name_key(DISQUALIFIED_NAME)
    assert all(asl.vendor_name_key(spelling) == key for spelling in TWIN_SPELLINGS)
    assert asl.vendor_name_key("Cinder- Forge") == asl.vendor_name_key("Cinder-Forge")
    assert asl.vendor_name_key("R.T.Hollis") == asl.vendor_name_key("R.T. Hollis")
    # Nothing fuzzier: a different word is a different supplier.
    assert asl.vendor_name_key("Acme Plating East") != key


# =========================================================================== POST /purchasing/vendors


@pytest.mark.parametrize("spelling", TWIN_SPELLINGS)
def test_creating_a_vendor_named_like_a_disqualified_one_is_refused_409(
    client, db_session, disqualified, manager_headers, spelling
):
    before = _vendor_create_audit_rows(db_session)
    response = client.post(VENDORS, headers=manager_headers, json={"code": f"TWN{_next():05d}", "name": spelling})
    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"] == TWIN_TEXT.format(DISQUALIFIED_NAME)
    assert [v.id for v in _vendors_named(db_session, "plating")] == [disqualified.id], "nothing was created"
    assert _vendor_create_audit_rows(db_session) == before, "no audit row for a refused create"


def test_the_refusal_holds_for_a_switched_off_disqualified_vendor(client, db_session, manager_headers):
    """Deactivating a vendor does not lift its disqualification, so it does not free its name."""
    off = make_vendor(db_session, name="Dormant Anodize Co", approval_status="disqualified", is_active=False)
    response = client.post(
        VENDORS, headers=manager_headers, json={"code": f"TWN{_next():05d}", "name": "dormant anodize co"}
    )
    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"] == TWIN_TEXT.format(off.name)


def test_positive_controls_a_new_name_and_names_that_are_not_disqualified_are_created(
    client, db_session, disqualified, manager_headers
):
    # A name that differs in a word is another supplier.
    other = client.post(
        VENDORS, headers=manager_headers, json={"code": f"TWN{_next():05d}", "name": "Acme Plating East Works 2"}
    )
    assert other.status_code == status.HTTP_200_OK, other.text

    # The namesake of a Conditional / Approved / not-on-the-ASL vendor is not refused: only a
    # Disqualified record is protected from a twin.
    for status_value in ("conditional", "approved", None):
        vendor = make_vendor(db_session, name=f"Namesake Source {_next()}", approval_status=status_value)
        twin = client.post(
            VENDORS, headers=manager_headers, json={"code": f"TWN{_next():05d}", "name": vendor.name.upper()}
        )
        assert twin.status_code == status.HTTP_200_OK, (status_value, twin.text)

    # A soft-deleted Disqualified vendor is not live (lead decision: "a live vendor").
    gone = make_vendor(db_session, name="Removed Platers Ltd", approval_status="disqualified", is_deleted=True)
    after_delete = client.post(VENDORS, headers=manager_headers, json={"code": f"TWN{_next():05d}", "name": gone.name})
    assert after_delete.status_code == status.HTTP_200_OK, after_delete.text


def test_another_companys_disqualified_vendor_frees_nothing_and_blocks_nothing(client, db_session, manager_headers):
    make_vendor(db_session, company_id=2, name="Foreign Plating Inc", approval_status="disqualified")
    response = client.post(
        VENDORS, headers=manager_headers, json={"code": f"TWN{_next():05d}", "name": "Foreign Plating Inc"}
    )
    assert response.status_code == status.HTTP_200_OK, response.text


# =========================================================================== PO upload


def _upload_payload(part, **fields) -> dict:
    payload = {
        "po_number": f"PO-TWN-{_next():05d}",
        "vendor_id": 0,  # what the review screen sends with create_vendor
        "create_vendor": True,
        "new_vendor_name": TWIN_SPELLINGS[0],
        "line_items": [{"part_id": part.id, "part_number": part.part_number, "quantity_ordered": 1, "unit_price": 1.0}],
        "create_parts": [],
        "pdf_path": "",
    }
    payload.update(fields)
    return payload


def test_po_upload_refuses_to_create_a_twin_and_raises_no_order(client, db_session, disqualified, manager_headers):
    part = make_part(db_session)
    response = client.post("/api/v1/po-upload/create-from-upload", headers=manager_headers, json=_upload_payload(part))
    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"] == TWIN_TEXT.format(DISQUALIFIED_NAME)
    db_session.rollback()
    assert db_session.query(PurchaseOrder).count() == 0
    assert [v.id for v in _vendors_named(db_session, "plating")] == [disqualified.id]

    # Positive control: the same upload naming a new supplier creates it and the order.
    created = client.post(
        "/api/v1/po-upload/create-from-upload",
        headers=manager_headers,
        json=_upload_payload(part, new_vendor_name="Brand New Platers"),
    )
    assert created.status_code == status.HTTP_200_OK, created.text
    assert created.json()["vendor_created"] is True
    db_session.rollback()
    assert db_session.query(PurchaseOrder).count() == 1


# =========================================================================== vendor CSV import


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "commit"])
def test_the_vendor_import_reports_a_twin_row_and_imports_the_rest(
    client, db_session, disqualified, manager_headers, dry_run
):
    csv = f"code,name\nTWN{_next():05d},{TWIN_SPELLINGS[1]}\nTWN{_next():05d},Fresh Coatings LLC\n".encode()
    response = client.post(
        f"{VENDORS}/import-csv?dry_run={'true' if dry_run else 'false'}",
        headers=manager_headers,
        files={"file": ("vendors.csv", io.BytesIO(csv), "text/csv")},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert (body["imported_count"], body["skipped_count"], body["total_rows"]) == (1, 1, 2)
    (error,) = body["errors"]
    assert error["row"] == 2 and error["reason"] == TWIN_TEXT.format(DISQUALIFIED_NAME)
    assert [v.id for v in _vendors_named(db_session, "plating")] == [disqualified.id], "no twin, ever"
    assert len(_vendors_named(db_session, "Fresh Coatings")) == (0 if dry_run else 1)


def test_the_import_center_vendor_batch_reports_the_twin_row(client, db_session, disqualified, admin_headers):
    """The Import Center's vendor batch runs the same handler, so it carries the same refusal."""
    csv = f"code,name\nTWN{_next():05d},{TWIN_SPELLINGS[2]}\nTWN{_next():05d},Batch Coatings LLC\n".encode()
    prepared = client.post(
        "/api/v1/import/batches/prepare",
        headers=admin_headers,
        data={"entity": "vendors", "request_key": str(uuid4())},
        files={"file": ("vendors.csv", csv, "text/csv")},
    )
    assert prepared.status_code == status.HTTP_200_OK, prepared.text
    review = prepared.json()
    assert TWIN_TEXT.format(DISQUALIFIED_NAME) in prepared.text, review
    commit = client.post(
        f"/api/v1/import/batches/{review['id']}/commit",
        headers=admin_headers,
        data={"expected_version": review["version"]},
    )
    assert commit.status_code == status.HTTP_200_OK, commit.text
    assert [v.id for v in _vendors_named(db_session, "plating")] == [disqualified.id]
    assert len(_vendors_named(db_session, "Batch Coatings")) == 1
