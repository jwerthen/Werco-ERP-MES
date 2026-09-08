"""Independent API contract tests: exact cost inputs, tenant access and no writes."""

from copy import deepcopy
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.core.security import create_access_token
from app.models.company import Company
from app.models.quote_config import MaterialCategory, QuoteMaterial
from app.models.role_permission import RolePermission
from app.models.user import UserRole

pytestmark = [pytest.mark.api, pytest.mark.integration]
BASE = "/api/v1/quote-nesting"


def material(db, **changes):
    values = dict(
        company_id=1,
        name="A36 carbon steel",
        category=MaterialCategory.STEEL,
        stock_price_per_pound=0.9,
        stock_price_per_cubic_inch=0.2,
        density_lb_per_cubic_inch=0.284,
        sheet_pricing={"1/8": 5.5, "0.25": 9.0},
        is_active=True,
    )
    values.update(changes)
    row = QuoteMaterial(**values)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def payload(row, **changes):
    value = dict(
        catalog_material_id=row.id,
        thickness_in="0.125",
        stock_options=[dict(id="48x96", width_in="48", length_in="96")],
        price_basis="per_lb",
    )
    value.update(changes)
    return value


def resolve(client, headers, value):
    response = client.post(BASE + "/material-resolution", json=value, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_fresh_company_reads_never_seed_or_mutate(client, admin_headers, db_session):
    writes = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split(None, 1)[0].upper() in {
            "INSERT",
            "UPDATE",
            "DELETE",
            "CREATE",
            "ALTER",
            "DROP",
        }:
            writes.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", observe)
    try:
        response = client.get(BASE + "/materials", headers=admin_headers)
        assert response.status_code == 200
        assert response.json() == {
            "schema_version": 1,
            "items": [],
            "total": 0,
            "offset": 0,
            "limit": 200,
        }
        missing = client.post(
            BASE + "/material-resolution",
            headers=admin_headers,
            json=payload(type("Missing", (), {"id": 999})()),
        )
        assert missing.status_code == 404
        assert writes == []
    finally:
        event.remove(db_session.bind, "before_cursor_execute", observe)


def test_two_selected_materials_keep_independent_prices_and_unconfirmed_provenance(client, admin_headers, db_session):
    carbon = material(db_session)
    stainless = material(
        db_session,
        name="304 stainless steel",
        category=MaterialCategory.STAINLESS,
        stock_price_per_pound=2.3,
        density_lb_per_cubic_inch=0.289,
    )
    first = resolve(client, admin_headers, payload(carbon))
    second = resolve(client, admin_headers, payload(stainless, thickness_in="0.25"))
    assert first["stocks"][0]["weight_lb"] == "163.584"
    assert first["stocks"][0]["sheet_cost"] == "147.23"
    assert second["stocks"][0]["weight_lb"] == "332.928"
    assert second["stocks"][0]["sheet_cost"] == "765.73"
    for result, row in [(first, carbon), (second, stainless)]:
        assert result["catalog_material"]["id"] == row.id
        assert result["status"] == "review_required"
        assert result["calculable"] is True and result["confirmed"] is False
        assert result["currency"] is None
        fields = {issue["field"] for issue in result["issues"]}
        assert {
            "currency",
            "grade",
            "coating",
            "certification",
            "inventory_mapping",
            "price_effective_date",
            "price_expiry",
            "approved_revision",
            "authoritative_thickness",
        } <= fields
    assert first["content_hash"] != second["content_hash"]


@pytest.mark.parametrize(
    "basis,key,expected",
    [
        ("per_lb", None, "147.23"),
        ("per_cubic_inch", None, "115.20"),
        ("per_square_foot", "1/8", "176.00"),
    ],
)
def test_explicit_price_bases_and_units(client, admin_headers, db_session, basis, key, expected):
    row = material(db_session)
    result = resolve(
        client,
        admin_headers,
        payload(row, price_basis=basis, **({"price_key": key} if key else {})),
    )
    stock = result["stocks"][0]
    assert stock["area_sq_in"] == "4608"
    assert stock["area_sq_ft"] == "32"
    assert stock["volume_cu_in"] == "576"
    assert stock["sheet_cost"] == expected


def test_exact_sheet_price_key_never_uses_nearest_or_first_key(client, admin_headers, db_session):
    row = material(db_session)
    result = resolve(
        client,
        admin_headers,
        payload(row, price_basis="per_square_foot", price_key="0.125"),
    )
    assert result["calculable"] is False
    assert result["status"] == "unresolved"
    assert result["stocks"][0]["sheet_cost"] is None
    assert "selected_price_missing_or_invalid" in {issue["code"] for issue in result["issues"]}
    # Even a selected existing key does not prove that its gauge matches the requested thickness.
    selected = resolve(
        client,
        admin_headers,
        payload(row, price_basis="per_square_foot", price_key="0.25"),
    )
    assert selected["stocks"][0]["sheet_cost"] == "288.00"
    assert "sheet_price_thickness_unverified" in {issue["code"] for issue in selected["issues"]}


@pytest.mark.parametrize("density", [None, 0, -1])
@pytest.mark.parametrize(
    "basis,key,cost",
    [
        ("per_lb", None, None),
        ("per_cubic_inch", None, "115.20"),
        ("per_square_foot", "1/8", "176.00"),
    ],
)
def test_missing_density_is_never_defaulted(client, admin_headers, db_session, density, basis, key, cost):
    row = material(db_session, density_lb_per_cubic_inch=density)
    # SQLAlchemy's insert default replaces explicit None; persist a legacy NULL separately.
    if density is None:
        row.density_lb_per_cubic_inch = None
        db_session.commit()
    result = resolve(
        client,
        admin_headers,
        payload(row, price_basis=basis, **({"price_key": key} if key else {})),
    )
    assert result["stocks"][0]["weight_lb"] is None
    assert result["stocks"][0]["sheet_cost"] == cost
    assert result["calculable"] is (cost is not None)
    assert "density_missing_or_invalid" in {issue["code"] for issue in result["issues"]}


@pytest.mark.parametrize("price", [0, -2, "NaN", "Infinity", True, "garbage", "1e999999999"])
def test_invalid_selected_source_price_remains_unresolved(client, admin_headers, db_session, price):
    row = material(db_session, sheet_pricing={"chosen": price})
    result = resolve(
        client,
        admin_headers,
        payload(row, price_basis="per_square_foot", price_key="chosen"),
    )
    assert result["calculable"] is False and result["confirmed"] is False
    assert result["stocks"][0]["sheet_cost"] is None
    assert result["catalog_material"]["price_options"][-1]["unit_price"] is None


def test_decimal_rounding_and_canonical_hash_replay(client, admin_headers, db_session):
    row = material(db_session, sheet_pricing={"exact": "2.005"})
    request = payload(
        row,
        price_basis="per_square_foot",
        price_key="exact",
        stock_options=[
            {"id": "b", "width_in": "12.000", "length_in": "12"},
            {"id": "a", "width_in": "1", "length_in": "1"},
        ],
    )
    first = resolve(client, admin_headers, request)
    assert first["stocks"][1]["sheet_cost"] == "2.01"
    assert first["stocks"][0]["sheet_cost"] == "0.01"
    reordered = deepcopy(request)
    reordered["thickness_in"] = "00.125000"
    reordered["stock_options"].reverse()
    reordered["stock_options"][1]["width_in"] = "12"
    second = resolve(client, admin_headers, reordered)
    assert first == second
    changed = resolve(client, admin_headers, {**request, "thickness_in": "0.25"})
    assert changed["content_hash"] != first["content_hash"]
    assert Decimal(first["stocks"][0]["area_sq_ft"]) == Decimal("0.006944444444")


def test_stale_source_hash_refuses_new_price_until_reloaded(client, admin_headers, db_session):
    row = material(db_session)
    listed = client.get(BASE + "/materials", headers=admin_headers).json()["items"][0]
    request = payload(row, expected_catalog_hash=listed["catalog_hash"])
    resolve(client, admin_headers, request)
    row.stock_price_per_pound = 1.2
    db_session.commit()
    response = client.post(BASE + "/material-resolution", headers=admin_headers, json=request)
    assert response.status_code == 409
    refreshed = client.get(BASE + "/materials", headers=admin_headers).json()["items"][0]
    assert refreshed["catalog_hash"] != listed["catalog_hash"]
    assert (
        resolve(
            client,
            admin_headers,
            {**request, "expected_catalog_hash": refreshed["catalog_hash"]},
        )["stocks"][
            0
        ]["sheet_cost"]
        == "196.30"
    )


def test_tenant_active_lifecycle_pagination_and_foreign_id_privacy(client, admin_headers, db_session):
    db_session.add(Company(id=2, name="Other", slug="other"))
    db_session.commit()
    own = material(db_session)
    inactive = material(db_session, name="Inactive", is_active=False)
    foreign = material(db_session, company_id=2, name="Other private alloy")
    second = material(db_session, name="Second own")
    response = client.get(BASE + "/materials?offset=1&limit=1", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert [row["id"] for row in response.json()["items"]] == [second.id]
    errors = [
        client.post(BASE + "/material-resolution", headers=admin_headers, json=payload(row))
        for row in [inactive, foreign, type("Missing", (), {"id": 999})()]
    ]
    assert [response.status_code for response in errors] == [404, 404, 404]
    assert errors[0].json() == errors[1].json() == errors[2].json()
    assert resolve(client, admin_headers, payload(own))["company_id"] == 1


def test_effective_role_overrides_are_tenant_scoped(client, operator_user, operator_headers, db_session):
    row = material(db_session)
    db_session.add(Company(id=2, name="Other", slug="other"))
    db_session.add(RolePermission(company_id=2, role=UserRole.OPERATOR, permissions=["purchasing:view"]))
    db_session.commit()
    assert client.get(BASE + "/materials", headers=operator_headers).status_code == 403
    override = RolePermission(company_id=1, role=UserRole.OPERATOR, permissions=["purchasing:view"])
    db_session.add(override)
    db_session.commit()
    assert client.get(BASE + "/materials", headers=operator_headers).status_code == 200
    resolve(client, operator_headers, payload(row))
    override.permissions = []
    db_session.commit()
    assert client.get(BASE + "/materials", headers=operator_headers).status_code == 403
    assert client.post(BASE + "/material-resolution", headers=operator_headers, json=payload(row)).status_code == 403


def test_default_manager_permission_can_be_revoked_only_by_own_company(client, auth_headers, db_session):
    db_session.add(Company(id=2, name="Other", slug="other"))
    db_session.add(RolePermission(company_id=2, role=UserRole.MANAGER, permissions=[]))
    db_session.commit()
    assert client.get(BASE + "/materials", headers=auth_headers).status_code == 200
    db_session.add(RolePermission(company_id=1, role=UserRole.MANAGER, permissions=[]))
    db_session.commit()
    assert client.get(BASE + "/materials", headers=auth_headers).status_code == 403


def test_active_company_switch_and_read_only_post_fence(client, admin_user, db_session):
    db_session.add(Company(id=2, name="Other", slug="other"))
    admin_user.role = UserRole.PLATFORM_ADMIN
    db_session.commit()
    material(db_session, name="Home catalog")
    foreign = material(db_session, company_id=2, name="Viewed company catalog")
    token = create_access_token(subject=admin_user.id, company_id=2, read_only=True)
    headers = {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}
    listed = client.get(BASE + "/materials", headers=headers)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["items"]] == [foreign.id]
    assert client.post(BASE + "/material-resolution", headers=headers, json=payload(foreign)).status_code == 403


@pytest.mark.parametrize(
    "value",
    [
        "0",
        "0.000",
        "-1",
        "NaN",
        "Infinity",
        "1e3",
        "1/8",
        "",
        " 1",
        "1.",
        "0.1234567890123",
        "1234567890123",
        0.125,
        True,
        None,
    ],
)
@pytest.mark.parametrize("field", ["thickness_in", "width_in", "length_in"])
def test_invalid_dimensions_rejected_at_api_boundary(client, admin_headers, db_session, field, value):
    request = payload(material(db_session))
    if field == "thickness_in":
        request[field] = value
    else:
        request["stock_options"][0][field] = value
    response = client.post(BASE + "/material-resolution", headers=admin_headers, json=request)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "change",
    [
        {"catalog_material_id": True},
        {"catalog_material_id": "1"},
        {"catalog_material_id": 0},
        {"stock_options": []},
        {"price_basis": "per_sheet"},
        {"price_basis": "per_square_foot"},
        {"price_key": "unexpected"},
        {"expected_catalog_hash": "bad"},
        {"company_id": 2},
    ],
)
def test_invalid_contract_inputs_rejected(client, admin_headers, db_session, change):
    response = client.post(
        BASE + "/material-resolution",
        headers=admin_headers,
        json=payload(material(db_session), **change),
    )
    assert response.status_code == 422


def test_successful_resolution_does_not_write_any_database_table(client, admin_headers, db_session):
    row = material(db_session)
    writes = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split(None, 1)[0].upper() in {
            "INSERT",
            "UPDATE",
            "DELETE",
        }:
            writes.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", observe)
    try:
        resolve(client, admin_headers, payload(row))
        client.get(BASE + "/materials", headers=admin_headers)
        assert writes == []
    finally:
        event.remove(db_session.bind, "before_cursor_execute", observe)


@pytest.mark.parametrize(
    "stocks",
    [
        [dict(id="duplicate", width_in="1", length_in="1")] * 2,
        [dict(id=" ", width_in="1", length_in="1")],
        [dict(id=str(index), width_in="1", length_in="1") for index in range(21)],
    ],
)
def test_duplicate_blank_and_excessive_stock_rows_rejected(client, admin_headers, db_session, stocks):
    response = client.post(
        BASE + "/material-resolution",
        headers=admin_headers,
        json=payload(material(db_session), stock_options=stocks),
    )
    assert response.status_code == 422


def test_authentication_and_kiosk_scope_still_protect_catalog(client, admin_user, db_session):
    row = material(db_session)
    assert client.get(BASE + "/materials").status_code in (401, 403)
    token = create_access_token(subject=admin_user.id, company_id=1, scope="kiosk")
    headers = {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}
    assert client.get(BASE + "/materials", headers=headers).status_code == 403
    assert client.post(BASE + "/material-resolution", headers=headers, json=payload(row)).status_code == 403
