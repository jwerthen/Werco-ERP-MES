"""Private-database integration checks for replacement fabrication quoting."""

import hashlib
import socket
from copy import deepcopy
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.api.endpoints import fabrication_quotes as endpoints
from app.core.security import create_access_token
from app.models.customer import Customer
from app.models.document_delivery import DocumentDelivery
from app.models.fabrication_quote import (
    FabricationQuote,
    FabricationQuoteActual,
    FabricationQuoteFile,
    FabricationQuoteRevision,
)
from app.models.quote import Quote, QuoteLine, QuoteStatus
from app.models.role_permission import RolePermission
from app.models.user import UserRole
from app.services.audit_service import AuditService, AuditWriteError
from app.services.fabrication_quote_service import digest
from tests.api.test_receiving_compliance import headers_for, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]
BASE = "/api/v1/fabrication-quotes"
REVIEW = {"reviewed": True, "source": "Synthetic independent API reference case"}


def ready_plan():
    return {
        "parts": [
            {
                "id": "A",
                "name": "Made assembly",
                "costing_complete": True,
                "evidence": REVIEW,
            },
            {
                "id": "B",
                "name": "Bought subassembly",
                "make_or_buy": "buy",
                "purchase_unit_cost": "20",
                "costing_complete": True,
                "evidence": REVIEW,
            },
        ],
        "roots": [{"part_id": "A", "quantity": "2"}],
        "bom": [{"id": "AB", "parent_id": "A", "child_id": "B", "quantity": "1"}],
        "materials": [
            {
                "id": "M",
                "part_id": "A",
                "consumed_quantity": "3",
                "unit_cost": "5",
                "evidence": REVIEW,
            }
        ],
        "operations": [
            {
                "id": "finish",
                "part_id": "A",
                "name": "Fixture and finishing",
                "setup_labor_seconds": "0",
                "setup_machine_seconds": "0",
                "labor_rate_per_hour": "36",
                "machine_rate_per_hour": "72",
                "consumables_cost_per_run": "2",
                "outside_cost_per_run": "3",
                "recipe": {
                    "kind": "manual",
                    "labor_seconds": "60",
                    "machine_seconds": "30",
                },
                "evidence": REVIEW,
            }
        ],
        "hardware": [
            {
                "id": "H",
                "part_id": "A",
                "manufacturer": "Synthetic",
                "mpn": "H-1",
                "quantity_per_part": "1",
                "stock_available": 10,
                "stock_unit_value": "1",
                "evidence": REVIEW,
            }
        ],
        "target_margin": "0.25",
    }


@pytest.fixture
def admin_headers(db_session):
    return headers_for(make_user(db_session, role=UserRole.ADMIN, company_id=1))


def create(client, headers, *, plan=None, customer_id=None, request_key=None):
    body = {
        "title": "Fabrication reference package",
        "plan": ready_plan() if plan is None else plan,
        "customer_id": customer_id,
    }
    if request_key:
        body["request_key"] = request_key
    response = client.post(BASE, headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def save(client, headers, row, **changes):
    body = {
        "expected_revision": row["revision"],
        "title": row["title"],
        "customer_id": row["customer_id"],
        "plan": row["plan"],
        **changes,
    }
    return client.put(f"{BASE}/{row['id']}", headers=headers, json=body)


def approve(client, headers, row):
    response = client.post(
        f"{BASE}/{row['id']}/approve",
        headers=headers,
        json={
            "expected_revision": row["revision"],
            "review_note": "Reviewed source, routes, quantities, costs and margin",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def customer(db, company_id=1):
    row = Customer(
        company_id=company_id,
        name=f"Fabrication customer {company_id}",
        email="customer@example.test",
    )
    db.add(row)
    db.commit()
    return row.id


def attach_source(client, headers, row, monkeypatch):
    content = b"manufacturer,mpn,quantity\nSynthetic,H-1,2\n"

    def analysis(raw, name, units):
        return {
            "file_name": name,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "kind": "csv",
            "status": "review_required",
            "parser": "test-csv",
            "observations": [{"kind": "bom_row", "row": 2, "mpn": "H-1"}],
            "issues": [],
        }

    monkeypatch.setattr(endpoints, "analyze_in_worker", analysis)
    response = client.post(
        f"{BASE}/{row['id']}/files",
        headers=headers,
        data={"expected_revision": str(row["revision"])},
        files={"file": ("../requirements.csv", content, "text/csv")},
    )
    assert response.status_code == 200, response.text
    return response.json(), content


def test_create_save_and_stale_conflicts_are_atomic(client, db_session, admin_headers):
    row = create(client, admin_headers, request_key="create-ref-1")
    assert row["status"] == "draft" and row["revision"] == 1
    assert row["calculation"]["totals"]["total_cost"] == "84.400000"
    retry = create(client, admin_headers, request_key="create-ref-1")
    assert retry["id"] == row["id"]
    assert db_session.query(FabricationQuoteRevision).count() == 1
    changed = save(client, admin_headers, row, title="Reviewed package")
    assert changed.status_code == 200
    assert changed.json()["revision"] == 2
    stale = save(client, admin_headers, row, title="Lost stale update")
    assert stale.status_code == 409
    current = client.get(f"{BASE}/{row['id']}", headers=admin_headers).json()
    assert current["title"] == "Reviewed package"
    assert db_session.query(FabricationQuoteRevision).count() == 2
    changed_request = client.post(
        BASE,
        headers=admin_headers,
        json={
            "title": "Different request",
            "plan": ready_plan(),
            "request_key": "create-ref-1",
        },
    )
    assert changed_request.status_code == 409


def test_missing_cost_and_human_review_block_approval(client, db_session, admin_headers):
    p = ready_plan()
    p["materials"][0]["unit_cost"] = None
    row = create(client, admin_headers, plan=p)
    response = client.post(
        f"{BASE}/{row['id']}/approve",
        headers=admin_headers,
        json={"expected_revision": 1, "review_note": "Reviewed"},
    )
    assert response.status_code == 422
    assert any(i["code"] == "unpriced_input" for i in response.json()["detail"]["issues"])
    assert db_session.query(FabricationQuote).one().revision == 1
    row = save(client, admin_headers, row, plan=ready_plan()).json()
    response = client.post(
        f"{BASE}/{row['id']}/approve",
        headers=admin_headers,
        json={"expected_revision": row["revision"], "review_note": " "},
    )
    assert response.status_code == 422
    approved = approve(client, admin_headers, row)
    assert save(client, admin_headers, approved, title="Illegal edit").status_code == 409


def test_file_evidence_requires_exact_review_and_original_content_is_scoped(
    client, db_session, admin_headers, monkeypatch
):
    row, content = attach_source(client, admin_headers, create(client, admin_headers), monkeypatch)
    source = row["files"][0]
    assert source["file_name"] == "requirements.csv"
    assert source["sha256"] == hashlib.sha256(content).hexdigest()
    assert source["analysis"]["observations"][0]["row"] == 2
    response = client.post(
        f"{BASE}/{row['id']}/approve",
        headers=admin_headers,
        json={"expected_revision": row["revision"], "review_note": "Checked quote"},
    )
    assert response.status_code == 422
    assert "source_review_required" in {x["code"] for x in response.json()["detail"]["issues"]}
    plan = deepcopy(row["plan"])
    plan["source_reviews"] = [
        {
            "file_id": source["id"],
            "sha256": "0" * 64,
            "disposition": "reviewed",
            "note": "BOM requirement recorded in hardware H",
        }
    ]
    wrong = save(client, admin_headers, row, plan=plan).json()
    assert not wrong["calculation"]["can_approve"]
    plan["source_reviews"][0]["sha256"] = source["sha256"]
    reviewed = save(client, admin_headers, wrong, plan=plan).json()
    approved = approve(client, admin_headers, reviewed)
    fetched = client.get(f"{BASE}/{row['id']}/files/{source['id']}/content", headers=admin_headers)
    assert fetched.status_code == 200 and fetched.content == content
    assert fetched.headers["x-content-type-options"] == "nosniff"
    assert fetched.headers["cache-control"] == "private, no-store"
    export = client.get(f"{BASE}/{row['id']}/export", headers=admin_headers)
    assert export.status_code == 200
    assert export.json()["snapshot"]["approved_revision"] == approved["approved_revision"]
    manifest = export.json()["snapshot"]["files"][0]
    assert "analysis" not in manifest
    assert manifest["analysis_sha256"] == digest(source["analysis"])
    assert manifest["parser"] == source["analysis"]["parser"]
    # Live review still includes full immutable extraction evidence.
    assert client.get(f"{BASE}/{row['id']}", headers=admin_headers).json()["files"][0]["analysis"] == source["analysis"]
    other = headers_for(make_user(db_session, role=UserRole.ADMIN, company_id=2))
    for suffix in [
        "",
        f"/files/{source['id']}/content",
        "/revisions",
        f"/revisions/{approved['approved_revision']}",
        "/export",
        "/actuals",
    ]:
        assert client.get(f"{BASE}/{row['id']}{suffix}", headers=other).status_code == 404
    assert client.get(BASE, headers=other).json()["items"] == []
    assert save(client, other, row).status_code == 404


def test_customer_reference_cannot_cross_tenants(client, db_session, admin_headers):
    make_user(db_session, role=UserRole.ADMIN, company_id=2)
    cid = customer(db_session, 2)
    response = client.post(
        BASE,
        headers=admin_headers,
        json={"title": "Cross tenant", "plan": ready_plan(), "customer_id": cid},
    )
    assert response.status_code == 422
    assert db_session.query(FabricationQuote).count() == 0


def test_effective_permissions_are_scoped_and_override_admin_defaults(client, db_session, admin_headers):
    row = create(client, admin_headers)
    operator = make_user(db_session, role=UserRole.OPERATOR, company_id=1)
    op_headers = headers_for(operator)
    assert client.get(BASE, headers=op_headers).status_code == 403
    make_user(db_session, role=UserRole.ADMIN, company_id=2)
    db_session.add(
        RolePermission(
            company_id=2,
            role=UserRole.OPERATOR,
            permissions=["purchasing:view", "purchasing:create"],
        )
    )
    db_session.commit()
    assert client.get(BASE, headers=op_headers).status_code == 403
    override = RolePermission(company_id=1, role=UserRole.OPERATOR, permissions=["purchasing:view"])
    db_session.add(override)
    db_session.commit()
    assert client.get(BASE, headers=op_headers).status_code == 200
    assert client.post(f"{BASE}/calculate", headers=op_headers, json={"plan": ready_plan()}).status_code == 200
    assert save(client, op_headers, row).status_code == 403
    override.permissions = ["purchasing:view", "purchasing:create"]
    db_session.commit()
    assert save(client, op_headers, row).status_code == 200
    db_session.add(RolePermission(company_id=1, role=UserRole.ADMIN, permissions=[]))
    db_session.commit()
    assert client.get(BASE, headers=admin_headers).status_code == 403


def test_read_only_company_context_can_view_but_cannot_mutate(client, db_session, admin_headers):
    row = create(client, admin_headers)
    platform = make_user(db_session, role=UserRole.PLATFORM_ADMIN, company_id=2)
    token = create_access_token(subject=platform.id, company_id=1, read_only=True)
    headers = {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}
    assert client.get(f"{BASE}/{row['id']}", headers=headers).status_code == 200
    assert save(client, headers, row).status_code == 403
    assert db_session.query(FabricationQuote).one().revision == 1


def test_audit_failure_rolls_back_create_and_save(client, db_session, admin_headers, monkeypatch):
    def fail(*args, **kwargs):
        raise AuditWriteError("synthetic audit failure")

    with monkeypatch.context() as patch:
        patch.setattr(AuditService, "log_required", fail)
        response = client.post(BASE, headers=admin_headers, json={"title": "Unsaved", "plan": ready_plan()})
        assert response.status_code == 503
    assert db_session.query(FabricationQuote).count() == 0
    assert db_session.query(FabricationQuoteRevision).count() == 0
    row = create(client, admin_headers)
    with monkeypatch.context() as patch:
        patch.setattr(AuditService, "log_required", fail)
        assert save(client, admin_headers, row, title="Unsaved change").status_code == 503
    db_session.expire_all()
    assert db_session.query(FabricationQuote).one().title == row["title"]
    assert db_session.query(FabricationQuote).one().revision == 1
    assert db_session.query(FabricationQuoteRevision).count() == 1


def test_approved_snapshot_is_immutable_in_database_and_survives_revise(client, db_session, admin_headers):
    approved = approve(client, admin_headers, create(client, admin_headers))
    revision = approved["approved_revision"]
    before = client.get(f"{BASE}/{approved['id']}/revisions/{revision}", headers=admin_headers).json()
    for statement in [
        "UPDATE fabrication_quote_revisions SET note='tampered' WHERE quote_id=:id",
        "DELETE FROM fabrication_quote_revisions WHERE quote_id=:id",
    ]:
        with pytest.raises(DBAPIError):
            db_session.execute(text(statement), {"id": approved["id"]})
        db_session.rollback()
    revised = client.post(
        f"{BASE}/{approved['id']}/revise",
        headers=admin_headers,
        json={
            "expected_revision": approved["revision"],
            "review_note": "New price basis",
        },
    )
    assert revised.status_code == 200
    draft = revised.json()
    assert draft["status"] == "draft" and draft["approved_revision"] is None
    changed = deepcopy(draft["plan"])
    changed["materials"][0]["unit_cost"] = "10"
    assert save(client, admin_headers, draft, plan=changed).status_code == 200
    assert client.get(f"{BASE}/{approved['id']}/revisions/{revision}", headers=admin_headers).json() == before


def test_handoff_retry_creates_one_erp_draft_with_complete_cost_and_no_egress(
    client, db_session, admin_headers, monkeypatch
):
    cid = customer(db_session)
    approved = approve(client, admin_headers, create(client, admin_headers, customer_id=cid))

    def no_network(*args, **kwargs):
        raise AssertionError("Handoff must not contact external services")

    monkeypatch.setattr(socket, "create_connection", no_network)
    body = {"expected_revision": approved["revision"]}
    first = client.post(f"{BASE}/{approved['id']}/handoff", headers=admin_headers, json=body)
    assert first.status_code == 200, first.text
    second = client.post(f"{BASE}/{approved['id']}/handoff", headers=admin_headers, json=body)
    assert second.status_code == 200
    assert first.json()["erp_quote_id"] == second.json()["erp_quote_id"]
    assert db_session.query(Quote).count() == 1
    erp = db_session.query(Quote).one()
    assert erp.status == QuoteStatus.DRAFT
    assert db_session.query(DocumentDelivery).count() == 0
    line = db_session.query(QuoteLine).one()
    assert line.quantity == 1
    assert line.material_cost == pytest.approx(72)
    assert line.labor_cost == pytest.approx(1.2)
    assert line.overhead_cost == pytest.approx(11.2)
    assert line.material_cost + line.labor_cost + line.overhead_cost == pytest.approx(84.4)
    assert erp.total == pytest.approx(float(approved["calculation"]["totals"]["selling_price"]))


def observation(revision, **changes):
    return {
        "request_key": "actual-run-1",
        "quote_revision": revision,
        "operation_id": "finish",
        "observed_on": str(date.today()),
        "good_quantity": "2",
        "scrap_quantity": "0",
        "setup_labor_seconds": "15",
        "run_labor_seconds": "140",
        "machine_seconds": "65",
        "source": "Timed internal shop trial",
        "note": "Synthetic actual reference",
        "completeness": "complete",
        **changes,
    }


def test_manual_actuals_use_approved_operation_revision_and_idempotent_key(client, db_session, admin_headers):
    row = create(client, admin_headers)
    assert client.post(f"{BASE}/{row['id']}/actuals", headers=admin_headers, json=observation(1)).status_code == 422
    approved = approve(client, admin_headers, row)
    revision = approved["approved_revision"]
    for changes in [
        {"operation_id": "missing"},
        {"observed_on": str(date.today() + timedelta(days=1))},
    ]:
        assert (
            client.post(
                f"{BASE}/{row['id']}/actuals",
                headers=admin_headers,
                json=observation(revision, **changes),
            ).status_code
            == 422
        )
    body = observation(revision)
    first = client.post(f"{BASE}/{row['id']}/actuals", headers=admin_headers, json=body)
    assert first.status_code == 200, first.text
    again = client.post(f"{BASE}/{row['id']}/actuals", headers=admin_headers, json=body)
    assert again.json()["id"] == first.json()["id"]
    body["run_labor_seconds"] = "999"
    assert client.post(f"{BASE}/{row['id']}/actuals", headers=admin_headers, json=body).status_code == 409
    assert db_session.query(FabricationQuoteActual).count() == 1
    revised = client.post(
        f"{BASE}/{row['id']}/revise",
        headers=admin_headers,
        json={"expected_revision": approved["revision"], "review_note": "Update route"},
    )
    assert revised.status_code == 200
    late = client.post(
        f"{BASE}/{row['id']}/actuals",
        headers=admin_headers,
        json=observation(revision, request_key="actual-run-2"),
    )
    assert late.status_code == 200
    assert late.json()["quote_revision"] == revision
    assert len(client.get(f"{BASE}/{row['id']}/actuals", headers=admin_headers).json()["items"]) == 2


@pytest.mark.parametrize("action", ["approve", "handoff", "actuals"])
def test_audit_failure_rolls_back_each_release_or_observation_action(
    client, db_session, admin_headers, monkeypatch, action
):
    row = create(client, admin_headers, customer_id=customer(db_session))
    if action != "approve":
        row = approve(client, admin_headers, row)
    prior_count = db_session.query(FabricationQuoteRevision).count()
    body = (
        observation(row["approved_revision"])
        if action == "actuals"
        else {"expected_revision": row["revision"], "review_note": "Reviewed package"}
    )

    def fail(*args, **kwargs):
        raise AuditWriteError("Synthetic required audit failure")

    with monkeypatch.context() as patch:
        patch.setattr(AuditService, "log_required", fail)
        response = client.post(f"{BASE}/{row['id']}/{action}", headers=admin_headers, json=body)
        assert response.status_code == 503, response.text
    db_session.expire_all()
    persisted = db_session.query(FabricationQuote).one()
    assert persisted.revision == row["revision"]
    assert persisted.status == row["status"]
    assert persisted.erp_quote_id is None
    assert db_session.query(FabricationQuoteRevision).count() == prior_count
    assert db_session.query(Quote).count() == 0
    assert db_session.query(QuoteLine).count() == 0
    assert db_session.query(FabricationQuoteActual).count() == 0


def test_inactive_bought_child_operation_is_not_an_actuals_target(client, db_session, admin_headers):
    p = ready_plan()
    skipped = deepcopy(p["operations"][0])
    skipped.update(id="supplier-welding", part_id="B")
    p["operations"].append(skipped)
    approved = approve(client, admin_headers, create(client, admin_headers, plan=p))
    response = client.post(
        f"{BASE}/{approved['id']}/actuals",
        headers=admin_headers,
        json=observation(approved["approved_revision"], operation_id="supplier-welding"),
    )
    assert response.status_code == 422
    assert db_session.query(FabricationQuoteActual).count() == 0


def test_complete_actual_cannot_hide_unknown_times_or_blank_evidence(client, db_session, admin_headers):
    approved = approve(client, admin_headers, create(client, admin_headers))
    for changes in [{"source": " "}, {"note": " "}, {"run_labor_seconds": None}]:
        response = client.post(
            f"{BASE}/{approved['id']}/actuals",
            headers=admin_headers,
            json=observation(approved["approved_revision"], **changes),
        )
        assert response.status_code == 422
    response = client.post(
        f"{BASE}/{approved['id']}/actuals",
        headers=admin_headers,
        json=observation(
            approved["approved_revision"],
            run_labor_seconds=None,
            completeness="partial",
        ),
    )
    assert response.status_code == 200
    assert response.json()["run_labor_seconds"] is None


def test_file_and_actual_evidence_are_immutable_even_through_direct_sql(client, db_session, admin_headers, monkeypatch):
    draft, _ = attach_source(client, admin_headers, create(client, admin_headers), monkeypatch)
    source = draft["files"][0]
    p = draft["plan"]
    p["source_reviews"] = [
        {
            "file_id": source["id"],
            "sha256": source["sha256"],
            "disposition": "reviewed",
            "note": "Recorded BOM quantity",
        }
    ]
    reviewed = save(client, admin_headers, draft, plan=p).json()
    approved = approve(client, admin_headers, reviewed)
    recorded = client.post(
        f"{BASE}/{draft['id']}/actuals",
        headers=admin_headers,
        json=observation(approved["approved_revision"]),
    )
    assert recorded.status_code == 200
    for table, field in [
        ("fabrication_quote_files", "file_name"),
        ("fabrication_quote_actuals", "request_key"),
    ]:
        # The table and field names are constant test cases, never request data.
        for statement in [
            f"UPDATE {table} SET {field}='tampered' WHERE quote_id=:id",
            f"DELETE FROM {table} WHERE quote_id=:id",
        ]:
            with pytest.raises(DBAPIError):
                db_session.execute(text(statement), {"id": draft["id"]})
            db_session.rollback()
    assert db_session.query(FabricationQuoteFile).one().sha256 == source["sha256"]
    assert db_session.query(FabricationQuoteActual).one().request_key == "actual-run-1"


def test_handoff_requires_customer_and_supported_erp_currency(client, db_session, admin_headers):
    approved = approve(client, admin_headers, create(client, admin_headers))
    assert (
        client.post(
            f"{BASE}/{approved['id']}/handoff",
            headers=admin_headers,
            json={"expected_revision": approved["revision"]},
        ).status_code
        == 422
    )
    p = ready_plan()
    p["currency"] = "EUR"
    non_usd = approve(
        client,
        admin_headers,
        create(client, admin_headers, plan=p, customer_id=customer(db_session)),
    )
    result = client.post(
        f"{BASE}/{non_usd['id']}/handoff",
        headers=admin_headers,
        json={"expected_revision": non_usd["revision"]},
    )
    assert result.status_code == 422
    assert db_session.query(Quote).count() == 0


def test_duplicate_file_upload_is_idempotent_without_evidence_replacement(
    client, db_session, admin_headers, monkeypatch
):
    first, content = attach_source(client, admin_headers, create(client, admin_headers), monkeypatch)
    again, _ = attach_source(client, admin_headers, first, monkeypatch)
    assert again["revision"] == first["revision"]
    assert len(again["files"]) == 1
    assert db_session.query(FabricationQuoteFile).count() == 1
    unrelated = create(client, admin_headers)
    response = client.get(
        f"{BASE}/{unrelated['id']}/files/{first['files'][0]['id']}/content",
        headers=admin_headers,
    )
    assert response.status_code == 404
