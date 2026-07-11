"""
Reproduction for the production 500 cluster: POST /quotes/, /purchasing/purchase-orders,
and /quality/ncr all returned 500 on valid input while Customer/Part/WO/Vendor creates
worked. These drive each create with a minimal VALID payload and assert a 2xx.
"""

import app.models as m
from app.core.security import create_access_token


def _headers(admin_user):
    token = create_access_token(subject=admin_user.id, company_id=admin_user.company_id)
    return {"Authorization": f"Bearer {token}"}


def _seed_vendor_and_part(db_session):
    vendor = m.Vendor(code="V-REPRO", name="Repro Vendor", company_id=1)
    part = m.Part(part_number="P-REPRO", name="Repro Part", part_type="manufactured", company_id=1)
    db_session.add_all([vendor, part])
    db_session.commit()
    db_session.refresh(vendor)
    db_session.refresh(part)
    return vendor, part


def test_create_quote(client, admin_user, db_session):
    resp = client.post(
        "/api/v1/quotes/",
        headers=_headers(admin_user),
        json={"customer_name": "Repro Co", "lines": [{"description": "Widget", "quantity": 3, "unit_price": 99.5}]},
    )
    assert resp.status_code in (200, 201), resp.text


def test_create_purchase_order(client, admin_user, db_session):
    vendor, part = _seed_vendor_and_part(db_session)
    resp = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=_headers(admin_user),
        json={
            "vendor_id": vendor.id,
            "required_date": "2026-07-24",
            "lines": [{"part_id": part.id, "quantity_ordered": 10, "unit_price": 3.5}],
        },
    )
    assert resp.status_code in (200, 201), resp.text


def test_add_quote_line(client, admin_user, db_session):
    # The add-line-to-existing-quote path builds a QuoteLine too — same missing-company_id risk.
    headers = _headers(admin_user)
    created = client.post("/api/v1/quotes/", headers=headers, json={"customer_name": "Repro Co", "lines": []})
    assert created.status_code in (200, 201), created.text
    quote_id = created.json()["id"]
    resp = client.post(
        f"/api/v1/quotes/{quote_id}/lines",
        headers=headers,
        json={"description": "Late-added widget", "quantity": 2, "unit_price": 50},
    )
    assert resp.status_code in (200, 201), resp.text


def test_create_ncr(client, admin_user, db_session):
    resp = client.post(
        "/api/v1/quality/ncr",
        headers=_headers(admin_user),
        json={
            "source": "in_process",
            "title": "Repro NCR title",
            "description": "A sufficiently long defect description for the reproduction test.",
        },
    )
    assert resp.status_code in (200, 201), resp.text
