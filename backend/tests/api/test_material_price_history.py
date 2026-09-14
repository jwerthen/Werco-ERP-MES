"""The price-history read contract: PO grain, effective permissions, and tenant isolation."""

from datetime import date, datetime, timedelta
from itertools import count

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.part import Part, PartType, UnitOfMeasure
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.role_permission import RolePermission
from app.models.user import UserRole
from tests.api.test_receiving_compliance import headers_for, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]
BASE = "/api/v1/purchasing/price-history"
_sequence = count(1)


def _part(
    db,
    *,
    company_id=1,
    name=None,
    part_type=PartType.RAW_MATERIAL,
    uom=UnitOfMeasure.SHEETS,
):
    n = next(_sequence)
    row = Part(
        company_id=company_id,
        part_number=f"MAT-{n:05}",
        name=name or f"Sheet {n}",
        part_type=part_type,
        unit_of_measure=uom,
    )
    db.add(row)
    db.flush()
    return row


def _vendor(db, *, company_id=1, name=None):
    n = next(_sequence)
    row = Vendor(company_id=company_id, code=f"V{n}", name=name or f"Vendor {n}")
    db.add(row)
    db.flush()
    return row


def _order(
    db,
    part,
    *,
    prices=((1, 10),),
    vendor=None,
    day=date(2026, 9, 1),
    po_status=POStatus.SENT,
    company_id=1,
):
    vendor = vendor or _vendor(db, company_id=company_id)
    row = PurchaseOrder(
        company_id=company_id,
        po_number=f"PO-{next(_sequence):05}",
        vendor_id=vendor.id,
        status=po_status,
        order_date=day,
        created_at=datetime(2026, 9, 1),
    )
    db.add(row)
    db.flush()
    for index, (quantity, price) in enumerate(prices, 1):
        db.add(
            PurchaseOrderLine(
                company_id=company_id,
                purchase_order_id=row.id,
                line_number=index,
                part_id=part.id,
                quantity_ordered=quantity,
                quantity_received=0,
                unit_price=price,
                line_total=999999,  # Historic cached line_total must not skew ordered costs.
            )
        )
    db.commit()
    return row


def _get(client, user, suffix="", **params):
    response = client.get(f"{BASE}{suffix}", headers=headers_for(user), params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_combines_duplicate_po_lines_before_comparing_and_preserves_page_baseline(
    client: TestClient, db_session: Session
):
    buyer = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    part = _part(db_session)
    earlier = _order(db_session, part, prices=((2, 10),), day=date(2026, 8, 1))
    latest = _order(db_session, part, prices=((3, 20), (1, 40)))

    overview = _get(client, buyer)
    assert overview["total"] == 1
    item = overview["items"][0]
    assert item["part_id"] == part.id
    assert item["latest_unit_price"] == 25
    assert item["previous_unit_price"] == 10
    assert item["price_change"] == 15
    assert item["price_change_percent"] == 150
    assert item["order_count"] == 2
    assert item["total_quantity"] == 6
    assert item["total_spend"] == 120
    assert item["currency"] is None
    assert item["unit_of_measure"] == "sheets"
    assert [p["purchase_order_id"] for p in item["sparkline"]] == [
        earlier.id,
        latest.id,
    ]

    detail = _get(client, buyer, f"/{part.id}", page_size=1)
    assert detail["total"] == 2
    assert detail["history"][0]["purchase_order_id"] == latest.id
    assert detail["history"][0]["line_count"] == 2
    assert detail["history"][0]["extended_price"] == 100
    assert detail["history"][0]["previous_unit_price"] == 10
    assert detail["stats"]["weighted_average_unit_price"] == 20
    assert detail["stats"]["lowest_unit_price"] == 10
    assert detail["stats"]["highest_unit_price"] == 25
    assert len(detail["chart"]) == 2
    second = _get(client, buyer, f"/{part.id}", page_size=1, page=2)
    assert second["history"][0]["purchase_order_id"] == earlier.id
    assert second["history"][0]["previous_unit_price"] is None
    assert second["stats"] == detail["stats"]
    assert any("historical PO units and currencies were not recorded" in note for note in detail["notes"])


def test_all_purchased_part_types_and_only_committed_live_orders(client, db_session):
    buyer = make_user(db_session, role=UserRole.VIEWER, company_id=1)
    for part_type in PartType:
        part = _part(db_session, part_type=part_type)
        part.is_active = False  # Purchase evidence survives an item being retired.
        _order(db_session, part)
    part = _part(db_session)
    for po_status in POStatus:
        _order(db_session, part, po_status=po_status)
    deleted = _order(db_session, part)
    deleted.is_deleted = True
    removed_part = _part(db_session)
    _order(db_session, removed_part)
    removed_part.is_deleted = True
    db_session.commit()

    response = _get(client, buyer)
    assert response["total"] == len(PartType) + 1
    assert {item["part_type"] for item in response["items"]} == {value.value for value in PartType}
    detail = _get(client, buyer, f"/{part.id}")
    assert detail["total"] == 5
    assert {row["status"] for row in detail["history"]} == {
        "approved",
        "sent",
        "partial",
        "received",
        "closed",
    }
    assert client.get(f"{BASE}/{removed_part.id}", headers=headers_for(buyer)).status_code == 404


def test_search_type_trends_sorting_and_counts_precede_pagination(client, db_session):
    buyer = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    for name, before, after, kind in [
        ("Aluminum up", 10, 12, PartType.RAW_MATERIAL),
        ("Aluminum down", 10, 8, PartType.RAW_MATERIAL),
        ("Aluminum same", 10, 10, PartType.RAW_MATERIAL),
        ("Aluminum hardware", 10, 30, PartType.HARDWARE),
    ]:
        part = _part(db_session, name=name, part_type=kind)
        _order(db_session, part, prices=((1, before),), day=date(2026, 8, 1))
        _order(db_session, part, prices=((1, after),))
    fresh = _part(db_session, name="Aluminum new 100%")
    _order(db_session, fresh)
    response = _get(
        client,
        buyer,
        search="aluminum",
        part_type="raw_material",
        trend="up",
        page_size=1,
    )
    assert response["total"] == 1
    assert response["items"][0]["part_name"] == "Aluminum up"
    assert response["summary"] == {
        "tracked_parts": 4,
        "price_increases": 1,
        "price_decreases": 1,
        "unchanged_parts": 1,
        "new_parts": 1,
    }
    assert _get(client, buyer, search="100%")["items"][0]["part_id"] == fresh.id
    assert _get(client, buyer, search="%")["total"] == 1
    assert _get(client, buyer, sort="increase", page_size=1)["items"][0]["part_name"] == "Aluminum hardware"
    assert _get(client, buyer, sort="decrease", page_size=1)["items"][0]["part_name"] == "Aluminum down"
    assert _get(client, buyer, trend="new")["items"][0]["part_id"] == fresh.id
    assert _get(client, buyer, trend="unchanged")["items"][0]["part_name"] == "Aluminum same"
    assert _get(client, buyer, page=99)["items"] == []
    assert _get(client, buyer, search="unavailable")["summary"]["tracked_parts"] == 0


def test_detail_filters_recompute_comparisons_and_keep_all_supplier_options(client, db_session):
    buyer = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    part = _part(db_session)
    vendor_a = _vendor(db_session, name="Alpha Metals")
    vendor_b = _vendor(db_session, name="Beta Metals")
    _order(db_session, part, vendor=vendor_a, prices=((2, 10),), day=date(2026, 7, 1))
    _order(db_session, part, vendor=vendor_b, prices=((100, 99),), day=date(2026, 8, 1))
    latest = _order(db_session, part, vendor=vendor_a, prices=((3, 20),), day=date(2026, 9, 1))
    vendor_a.is_deleted = True  # The retained PO still has supplier provenance.
    db_session.commit()

    filtered = _get(client, buyer, f"/{part.id}", vendor_id=vendor_a.id)
    assert filtered["stats"]["order_count"] == 2
    assert filtered["stats"]["previous_unit_price"] == 10
    assert filtered["stats"]["weighted_average_unit_price"] == 16
    assert filtered["history"][0]["price_change_percent"] == 100
    assert len(filtered["vendor_options"]) == 2
    assert all(point["vendor_name"] == "Alpha Metals" for point in filtered["chart"])
    dated = _get(
        client,
        buyer,
        f"/{part.id}",
        vendor_id=vendor_a.id,
        start_date="2026-09-01",
        end_date="2026-09-01",
    )
    assert dated["total"] == 1
    assert dated["history"][0]["purchase_order_id"] == latest.id
    assert dated["stats"]["previous_unit_price"] is None
    empty = _get(client, buyer, f"/{part.id}", start_date="2027-01-01")
    assert empty["part"]["part_id"] == part.id
    assert empty["total"] == empty["stats"]["total_spend"] == 0
    assert empty["stats"]["latest_unit_price"] is None
    assert empty["history"] == empty["chart"] == []
    assert len(empty["vendor_options"]) == 2


def test_zero_prices_are_valid_without_division_by_zero(client, db_session):
    buyer = make_user(db_session, role=UserRole.VIEWER, company_id=1)
    part = _part(db_session)
    _order(db_session, part, prices=((1, 0),), day=date(2026, 8, 1))
    _order(db_session, part, prices=((1, 10),))
    changed = _get(client, buyer, trend="up")["items"][0]
    assert changed["previous_unit_price"] == 0
    assert changed["price_change"] == 10
    assert changed["price_change_percent"] is None
    assert _get(client, buyer, trend="new")["total"] == 0
    zero = _part(db_session)
    _order(db_session, zero, prices=((1, 0),), day=date(2026, 8, 1))
    _order(db_session, zero, prices=((1, 0),))
    assert _get(client, buyer, trend="unchanged")["items"][0]["latest_unit_price"] == 0


def test_tenant_isolation_applies_to_every_join_even_corrupt_foreign_keys(client, db_session):
    buyer = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    make_user(db_session, role=UserRole.MANAGER, company_id=2)
    local = _part(db_session)
    remote = _part(db_session, company_id=2)
    _order(db_session, local)
    foreign_po = _order(db_session, remote, company_id=2)
    # Foreign part on a local PO cannot enter the list or detail.
    _order(db_session, remote)
    # Foreign vendor on a local PO cannot leak the vendor name.
    _order(db_session, local, vendor=_vendor(db_session, company_id=2))
    # Foreign line on a local PO cannot pollute ordered costs.
    wrong_line_po = _order(db_session, local)
    wrong_line_po.lines[0].company_id = 2
    # Local line cannot borrow another company's header.
    db_session.add(
        PurchaseOrderLine(
            company_id=1,
            part_id=local.id,
            purchase_order_id=foreign_po.id,
            line_number=2,
            quantity_ordered=1,
            quantity_received=0,
            unit_price=900,
        )
    )
    db_session.commit()

    listed = _get(client, buyer)
    assert listed["total"] == 1
    assert listed["items"][0]["order_count"] == 1
    assert listed["items"][0]["total_spend"] == 10
    detail = _get(client, buyer, f"/{local.id}")
    assert detail["total"] == len(detail["vendor_options"]) == 1
    assert client.get(f"{BASE}/{remote.id}", headers=headers_for(buyer)).status_code == 404


def test_permissions_honor_current_company_role_overrides_for_both_endpoints(client, db_session):
    viewer = make_user(db_session, role=UserRole.VIEWER, company_id=1)
    operator = make_user(db_session, role=UserRole.OPERATOR, company_id=1)
    make_user(db_session, role=UserRole.ADMIN, company_id=2)
    part = _part(db_session)
    _order(db_session, part)
    for suffix in ("", f"/{part.id}"):
        assert client.get(f"{BASE}{suffix}").status_code == 401
        assert client.get(f"{BASE}{suffix}", headers=headers_for(operator)).status_code == 403
        assert client.get(f"{BASE}{suffix}", headers=headers_for(viewer)).status_code == 200
    db_session.add_all(
        [
            RolePermission(company_id=1, role=UserRole.VIEWER, permissions=[]),
            RolePermission(company_id=2, role=UserRole.OPERATOR, permissions=["purchasing:view"]),
        ]
    )
    db_session.commit()
    for suffix in ("", f"/{part.id}"):
        assert client.get(f"{BASE}{suffix}", headers=headers_for(viewer)).status_code == 403
        assert client.get(f"{BASE}{suffix}", headers=headers_for(operator)).status_code == 403
    db_session.add(RolePermission(company_id=1, role=UserRole.OPERATOR, permissions=["purchasing:view"]))
    db_session.commit()
    assert _get(client, operator)["total"] == 1
    assert _get(client, operator, f"/{part.id}")["total"] == 1


def test_missing_order_date_uses_creation_date_and_same_day_ordering_is_stable(client, db_session):
    buyer = make_user(db_session, role=UserRole.VIEWER, company_id=1)
    part = _part(db_session)
    first = _order(db_session, part, day=None, prices=((1, 10),))
    second = _order(db_session, part, prices=((1, 20),))
    third = _order(db_session, part, prices=((1, 30),))
    # Creation timestamp wins before ID, even when this record has the older ID.
    second.created_at = datetime(2026, 9, 1, 1)
    db_session.commit()
    detail = _get(client, buyer, f"/{part.id}", start_date="2026-09-01", end_date="2026-09-01")
    assert [row["purchase_order_id"] for row in detail["history"]] == [
        second.id,
        third.id,
        first.id,
    ]
    assert detail["history"][-1]["order_date"] == "2026-09-01"
    assert detail["stats"]["latest_unit_price"] == 20
    assert detail["stats"]["previous_unit_price"] == 30


def test_undatable_orders_are_excluded_without_inventing_dates_or_breaking_other_items(client, db_session):
    buyer = make_user(db_session, role=UserRole.VIEWER, company_id=1)
    part = _part(db_session)
    dated = _order(db_session, part)
    undated = _order(db_session, part, day=None, prices=((1, 999),))
    undated.created_at = None
    only_undated_part = _part(db_session)
    only_undated = _order(db_session, only_undated_part, day=None)
    only_undated.created_at = None
    db_session.commit()

    overview = _get(client, buyer)
    assert overview["total"] == 1
    assert overview["items"][0]["latest_po_id"] == dated.id
    assert overview["items"][0]["latest_unit_price"] == 10
    detail = _get(client, buyer, f"/{part.id}")
    assert detail["total"] == 1
    assert detail["history"][0]["purchase_order_id"] == dated.id
    assert any("neither date are excluded" in note for note in detail["notes"])
    assert client.get(f"{BASE}/{only_undated_part.id}", headers=headers_for(buyer)).status_code == 404


@pytest.mark.parametrize(
    "quantity,price",
    [(float("inf"), 10), (1, float("inf")), (1e19, 10), (1, 1e19), (1e-20, 10)],
)
def test_unrepresentable_float_values_cannot_poison_aggregates(client, db_session, quantity, price):
    buyer = make_user(db_session, role=UserRole.VIEWER, company_id=1)
    part = _part(db_session)
    valid = _order(db_session, part)
    _order(db_session, part, prices=((quantity, price),))

    overview = _get(client, buyer)["items"][0]
    assert overview["latest_po_id"] == valid.id
    assert overview["order_count"] == 1
    assert overview["total_quantity"] == 1
    assert overview["total_spend"] == overview["latest_unit_price"] == 10
    detail = _get(client, buyer, f"/{part.id}")
    assert detail["total"] == 1
    assert detail["stats"]["weighted_average_unit_price"] == 10
    assert len(detail["chart"]) == 1
    assert any("unrepresentable quantities" in note for note in detail["notes"])


def test_sparkline_is_bounded_and_chart_truncation_does_not_truncate_table_or_stats(client, db_session, monkeypatch):
    from app.services import material_price_history_service as service

    monkeypatch.setattr(service, "CHART_LIMIT", 5)
    buyer = make_user(db_session, role=UserRole.VIEWER, company_id=1)
    part = _part(db_session)
    for index in range(15):
        _order(
            db_session,
            part,
            prices=((1, index + 1),),
            day=date(2026, 8, 1) + timedelta(days=index),
        )
    overview = _get(client, buyer)["items"][0]
    assert len(overview["sparkline"]) == 12
    assert [point["unit_price"] for point in overview["sparkline"]] == list(range(4, 16))
    detail = _get(client, buyer, f"/{part.id}", page=2, page_size=10)
    assert detail["chart_truncated"] is True
    assert [point["unit_price"] for point in detail["chart"]] == list(range(11, 16))
    assert detail["total"] == detail["stats"]["order_count"] == 15
    assert detail["stats"]["lowest_unit_price"] == 1
    assert detail["stats"]["weighted_average_unit_price"] == 8
    assert [row["unit_price"] for row in detail["history"]] == [5, 4, 3, 2, 1]


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"page_size": 101},
        {"trend": "bad"},
        {"sort": "bad"},
        {"part_type": "bad"},
    ],
)
def test_overview_rejects_invalid_filters(client, db_session, params):
    buyer = make_user(db_session, role=UserRole.VIEWER, company_id=1)
    assert client.get(BASE, params=params, headers=headers_for(buyer)).status_code == 422


def test_detail_rejects_reversed_or_invalid_dates_and_invalid_vendor(client, db_session):
    buyer = make_user(db_session, role=UserRole.VIEWER, company_id=1)
    for params in [
        {"start_date": "2026-09-02", "end_date": "2026-09-01"},
        {"start_date": "bad"},
        {"vendor_id": 0},
    ]:
        assert client.get(f"{BASE}/1", params=params, headers=headers_for(buyer)).status_code == 422
