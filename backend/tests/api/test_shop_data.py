"""Phase 5 tests: Shop Data Cut/Bend edit + job actuals."""

from __future__ import annotations

from app.models.estimate_workbench import CutBendRow, CutBendTable, CutBendTableKind
from app.models.quote_config import SettingsAuditLog
from app.models.rfq_quote import QuoteEstimate, RfqPackage
from app.services.estimate_workbench_service import seed_cut_bend_defaults


def test_shop_data_list_and_patch_requires_note(client, admin_headers, db_session):
    seed_cut_bend_defaults(db_session, company_id=1, force=True)
    db_session.commit()

    listed = client.get("/api/v1/estimate-workbench/shop-data", headers=admin_headers)
    assert listed.status_code == 200, listed.text
    tables = listed.json()["tables"]
    assert len(tables) == 5
    brake = next(t for t in tables if t["kind"] == CutBendTableKind.BRAKE_TIME.value)
    assert brake["rows"]
    row_id = brake["rows"][0]["id"]
    old_val = brake["rows"][0]["value"]

    # Missing note → 422 from pydantic min_length
    bad = client.patch(
        f"/api/v1/estimate-workbench/shop-data/brake_time/rows/{row_id}",
        headers=admin_headers,
        json={"value": 99},
    )
    assert bad.status_code == 422

    ok = client.patch(
        f"/api/v1/estimate-workbench/shop-data/brake_time/rows/{row_id}",
        headers=admin_headers,
        json={
            "value": float(old_val or 15) + 5,
            "note": "time study March 2026 — heavier handling",
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["value"] == float(old_val or 15) + 5

    audits = (
        db_session.query(SettingsAuditLog)
        .filter(SettingsAuditLog.entity_type == "cut_bend_row", SettingsAuditLog.entity_id == row_id)
        .all()
    )
    assert audits
    assert audits[-1].field_changed == "value"

    hist = client.get("/api/v1/estimate-workbench/shop-data/history", headers=admin_headers)
    assert hist.status_code == 200
    assert any(h["entity_id"] == row_id for h in hist.json())


def test_shop_data_add_row_sorts_by_thickness(client, admin_headers, db_session):
    seed_cut_bend_defaults(db_session, company_id=1, force=True)
    db_session.commit()

    created = client.post(
        "/api/v1/estimate-workbench/shop-data/pierce_time/rows",
        headers=admin_headers,
        json={
            "thickness_in": 0.200,
            "value": 1.2,
            "note": "added mid-band from nest study",
        },
    )
    assert created.status_code == 201, created.text

    listed = client.get("/api/v1/estimate-workbench/shop-data", headers=admin_headers)
    pierce = next(t for t in listed.json()["tables"] if t["kind"] == "pierce_time")
    thicknesses = [r["thickness_in"] for r in pierce["rows"] if r["thickness_in"] is not None]
    assert thicknesses == sorted(thicknesses)
    assert 0.200 in thicknesses


def test_job_actuals_upsert_and_tune_hint(client, admin_headers, db_session):
    pkg = RfqPackage(
        rfq_number="RFQ-ACT-1",
        customer_name="Act Customer",
        status="uploaded",
        company_id=1,
    )
    db_session.add(pkg)
    db_session.flush()
    est = QuoteEstimate(
        rfq_package_id=pkg.id,
        version=1,
        currency="USD",
        company_id=1,
        internal_breakdown={"laser_hours": 1.0, "brake_hours": 0.5, "weld_hours": 0.2},
    )
    db_session.add(est)
    db_session.commit()
    db_session.refresh(est)

    resp = client.post(
        "/api/v1/estimate-workbench/job-actuals",
        headers=admin_headers,
        json={
            "quote_estimate_id": est.id,
            "actual_laser_hours": 1.4,
            "actual_brake_hours": 0.5,
            "actual_weld_hours": 0.2,
            "notes": "nest ran slow on 3/8",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["quoted_laser_hours"] == 1.0
    assert abs(body["delta_laser_pct"] - 0.4) < 1e-6
    assert any(h["kind"] == "laser_speed" for h in body["propose_tune"])

    listed = client.get("/api/v1/estimate-workbench/job-actuals", headers=admin_headers)
    assert listed.status_code == 200
    assert any(r["quote_estimate_id"] == est.id for r in listed.json())
