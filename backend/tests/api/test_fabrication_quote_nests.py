"""Actual bounded nesting service integrated with saved quotation evidence."""

from copy import deepcopy

import pytest

from app.models.user import UserRole
from tests.api.test_fabrication_quotes import BASE, REVIEW, create, ready_plan, save
from tests.api.test_receiving_compliance import headers_for, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


def attach_nest(client, headers, row):
    response = client.post(
        f"{BASE}/{row['id']}/nests",
        headers=headers,
        json={
            "expected_revision": row["revision"],
            "input": {
                "parts": [
                    {"id": "A", "quantity": 2, "width_mm": 20, "height_mm": 10, "material": "CRS", "thickness_mm": 1}
                ],
                "stocks": [
                    {
                        "id": "sheet",
                        "quantity": 1,
                        "width_mm": 100,
                        "height_mm": 50,
                        "material": "CRS",
                        "thickness_mm": 1,
                        "price": "12",
                        "currency": "USD",
                    }
                ],
                "spacing_mm": 2,
                "edge_margin_mm": 2,
            },
        },
    )
    assert response.status_code == 200, response.text
    row = response.json()
    file = row["files"][0]
    assert file["analysis"]["geometry"]["validated"]
    row["plan"]["source_reviews"] = [
        {
            "file_id": file["id"],
            "sha256": file["sha256"],
            "disposition": "reviewed",
            "note": "Checked stock, grain, placements and part identity",
        }
    ]
    row["plan"]["materials"] = [
        {
            "id": "stock",
            "part_id": "A",
            "quantity_basis": "per_batch",
            "batch_size": "2",
            "consumed_quantity": "1",
            "unit": "sheet",
            "unit_cost": "12",
            "evidence": {**REVIEW, "source": f"nest:{file['id']}"},
        }
    ]
    response = save(client, headers, row)
    assert response.status_code == 200, response.text
    return response.json()


def test_saved_nest_cost_and_scenarios_use_exact_demand(client, db_session):
    headers = headers_for(make_user(db_session, role=UserRole.ADMIN, company_id=1))
    row = attach_nest(client, headers, create(client, headers))
    assert row["calculation"]["can_approve"]
    assert row["calculation"]["totals"]["material_cost"] == "12.000000"
    updated = deepcopy(row["plan"])
    updated["roots"][0]["quantity"] = "3"
    response = client.post(BASE + "/calculate", headers=headers, json={"quote_id": row["id"], "plan": updated})
    assert response.status_code == 200
    assert not response.json()["can_approve"]
    assert "stale_nest_quantity" in {i["code"] for i in response.json()["issues"]}
    response = save(client, headers, row, plan=updated)
    assert not response.json()["calculation"]["can_approve"]


def test_nest_cannot_be_double_charged_or_excluded_while_used(client, db_session):
    headers = headers_for(make_user(db_session, role=UserRole.ADMIN, company_id=1))
    row = attach_nest(client, headers, create(client, headers))
    plan = deepcopy(row["plan"])
    other = deepcopy(plan["materials"][0])
    other["id"] = "duplicate"
    plan["materials"].append(other)
    plan["source_reviews"][0]["disposition"] = "excluded"
    response = client.post(BASE + "/calculate", headers=headers, json={"quote_id": row["id"], "plan": plan})
    assert response.status_code == 200
    assert {"duplicate_nest_cost", "excluded_nest"}.issubset({i["code"] for i in response.json()["issues"]})


def test_source_context_cannot_cross_tenants(client, db_session):
    first = headers_for(make_user(db_session, role=UserRole.ADMIN, company_id=1))
    second = headers_for(make_user(db_session, role=UserRole.ADMIN, company_id=2))
    row = create(client, first)
    response = client.post(BASE + "/calculate", headers=second, json={"quote_id": row["id"], "plan": ready_plan()})
    assert response.status_code == 404


@pytest.mark.parametrize("change_make_to_buy", [False, True])
def test_nest_cost_cannot_disappear_at_a_purchase_boundary(client, db_session, change_make_to_buy):
    headers = headers_for(make_user(db_session, role=UserRole.ADMIN, company_id=1))
    row = attach_nest(client, headers, create(client, headers))
    assert row["calculation"]["can_approve"]
    assert row["calculation"]["totals"]["material_cost"] == "12.000000"
    plan = deepcopy(row["plan"])
    if change_make_to_buy:
        # The existing allocation stays on A while an estimator changes A's
        # route. Its rolled-up demand still matches the saved layout exactly.
        plan["parts"][0]["make_or_buy"] = "buy"
        plan["parts"][0]["purchase_unit_cost"] = "20"
    else:
        # B is a purchased child with the same demand as A. Matching quantity
        # alone must not authorize a material charge the engine will skip.
        plan["materials"][0]["part_id"] = "B"
    scenario = client.post(
        BASE + "/calculate",
        headers=headers,
        json={"quote_id": row["id"], "plan": plan},
    )
    assert scenario.status_code == 200
    assert not scenario.json()["can_approve"]
    assert "nest_cost_allocation" in {issue["code"] for issue in scenario.json()["issues"]}
    saved = save(client, headers, row, plan=plan)
    assert saved.status_code == 200
    saved_row = saved.json()
    assert saved_row["calculation"]["material_lines"] == []
    assert not saved_row["calculation"]["can_approve"]
    approval = client.post(
        f"{BASE}/{row['id']}/approve",
        headers=headers,
        json={
            "expected_revision": saved_row["revision"],
            "review_note": "Reviewed original nest, source files and costs",
        },
    )
    assert approval.status_code == 422
    assert "nest_cost_allocation" in {issue["code"] for issue in approval.json()["detail"]["issues"]}
