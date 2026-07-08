"""API tests for estimate workbench Phase 1 endpoints."""

from __future__ import annotations

from app.models.quote_config import MaterialCategory, QuoteMaterial
from app.models.rfq_quote import RfqPackage


def _rfq(db_session) -> RfqPackage:
    pkg = RfqPackage(
        rfq_number="RFQ-EW-API-001",
        customer_name="API Customer",
        status="uploaded",
        company_id=1,
    )
    db_session.add(pkg)
    db_session.commit()
    db_session.refresh(pkg)
    return pkg


def test_recalc_endpoint(client, admin_headers, db_session):
    resp = client.post(
        "/api/v1/estimate-workbench/recalc",
        headers=admin_headers,
        json={
            "assemblies": [
                {
                    "name": "A1",
                    "fab_lines": [
                        {
                            "detail_name": "Panel",
                            "material": "A36",
                            "qty": 1,
                            "thickness_in": 0.075,
                            "width_in": 10,
                            "length_in": 10,
                            "cut_length_in": 40,
                            "bend_count": 2,
                            "price_per_lb": 0.90,
                        }
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["shop_data_source"] in ("db", "defaults", "mixed")
    assert data["bid_summary"]["brake_hours"] > 0
    assert data["bid_summary"]["sell_price"] > data["bid_summary"]["cogs"]


def test_create_get_put_workbench(client, admin_headers, db_session):
    pkg = _rfq(db_session)
    db_session.add(
        QuoteMaterial(
            name="A36 Mild Steel",
            category=MaterialCategory.STEEL,
            stock_price_per_pound=0.90,
            density_lb_per_cubic_inch=0.284,
            is_active=True,
            company_id=1,
        )
    )
    db_session.commit()

    create = client.post(
        "/api/v1/estimate-workbench/",
        headers=admin_headers,
        json={"rfq_package_id": pkg.id},
    )
    assert create.status_code == 201, create.text
    created = create.json()
    estimate_id = created["estimate_id"]
    assert created["version"] == 1
    assert len(created["assemblies"]) == 1

    got = client.get(f"/api/v1/estimate-workbench/{estimate_id}", headers=admin_headers)
    assert got.status_code == 200
    assert got.json()["estimate_id"] == estimate_id

    save = client.put(
        f"/api/v1/estimate-workbench/{estimate_id}",
        headers=admin_headers,
        json={
            "version": 1,
            "assemblies": [
                {
                    "name": "TC-2-EXT",
                    "fab_lines": [
                        {
                            "detail_name": "Bottom",
                            "material": "A36 Mild Steel",
                            "qty": 10,
                            "thickness_in": 0.075,
                            "width_in": 12,
                            "length_in": 24,
                            "cut_length_in": 72,
                            "pierce_count": 2,
                            "bend_count": 4,
                        }
                    ],
                    "buyout_lines": [],
                }
            ],
            "machined_parts": [],
        },
    )
    assert save.status_code == 200, save.text
    saved = save.json()
    assert saved["version"] == 2
    assert saved["grand_total"] > 0
    assert saved["assemblies"][0]["name"] == "TC-2-EXT"
    fab = saved["assemblies"][0]["fab_lines"][0]
    assert fab["brake_hours"] == 0.2
    assert fab["brake_cost"] == 19.0

    # Stale version → 409
    stale = client.put(
        f"/api/v1/estimate-workbench/{estimate_id}",
        headers=admin_headers,
        json={"version": 1, "assemblies": [], "machined_parts": []},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_version"] == 2


def test_verification_and_finalize_gate(client, admin_headers, db_session):
    pkg = _rfq(db_session)
    db_session.add(
        QuoteMaterial(
            name="A36 Mild Steel",
            category=MaterialCategory.STEEL,
            stock_price_per_pound=0.90,
            density_lb_per_cubic_inch=0.284,
            is_active=True,
            company_id=1,
        )
    )
    db_session.commit()

    create = client.post(
        "/api/v1/estimate-workbench/",
        headers=admin_headers,
        json={"rfq_package_id": pkg.id},
    )
    estimate_id = create.json()["estimate_id"]

    # Save with Review confidence → finalize blocked
    client.put(
        f"/api/v1/estimate-workbench/{estimate_id}",
        headers=admin_headers,
        json={
            "version": 1,
            "assemblies": [
                {
                    "name": "A1",
                    "fab_lines": [
                        {
                            "detail_name": "Panel",
                            "material": "A36 Mild Steel",
                            "qty": 1,
                            "thickness_in": 0.075,
                            "width_in": 10,
                            "length_in": 10,
                            "cut_length_in": 40,
                            "bend_count": 2,
                            "confidence": "review",
                        }
                    ],
                    "buyout_lines": [],
                }
            ],
            "machined_parts": [],
        },
    )

    ver = client.get(
        f"/api/v1/estimate-workbench/{estimate_id}/verification",
        headers=admin_headers,
    )
    assert ver.status_code == 200
    assert ver.json()["can_finalize"] is False
    assert ver.json()["review_count"] >= 1

    blocked = client.post(
        f"/api/v1/estimate-workbench/{estimate_id}/finalize",
        headers=admin_headers,
        json={"valid_days": 30},
    )
    assert blocked.status_code == 422

    # Confirm the line → finalize succeeds
    client.put(
        f"/api/v1/estimate-workbench/{estimate_id}",
        headers=admin_headers,
        json={
            "version": 2,
            "assemblies": [
                {
                    "name": "A1",
                    "fab_lines": [
                        {
                            "detail_name": "Panel",
                            "material": "A36 Mild Steel",
                            "qty": 1,
                            "thickness_in": 0.075,
                            "width_in": 10,
                            "length_in": 10,
                            "cut_length_in": 40,
                            "bend_count": 2,
                            "confidence": "confirmed",
                            "verification_note": "Checked",
                        }
                    ],
                    "buyout_lines": [],
                }
            ],
            "machined_parts": [],
        },
    )
    ok = client.post(
        f"/api/v1/estimate-workbench/{estimate_id}/finalize",
        headers=admin_headers,
        json={"valid_days": 30},
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["quote_id"]
    assert body["quote_number"].startswith("QTE-")
    assert body["grand_total"] > 0
