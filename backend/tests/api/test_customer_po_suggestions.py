import pytest
from sqlalchemy import event

from app.models.company import Company
from app.models.part import Part
from app.models.role_permission import RolePermission
from app.models.work_order import WorkOrder, WorkOrderStatus

URL = "/api/v1/work-orders/customer-po-suggestions"


def add_order(db, part, number, **overrides):
    values = {
        "company_id": part.company_id,
        "part_id": part.id,
        "work_order_number": number,
        "customer_name": "Acme",
        "customer_po": "PO-100",
        "status": WorkOrderStatus.RELEASED,
        "quantity_ordered": 10,
    }
    values.update(overrides)
    db.add(WorkOrder(**values))


def suggestions(client, headers, **params):
    response = client.get(URL, headers=headers, params=params)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def test_prefix_is_case_insensitive_literal_and_matches_only_po_number(client, db_session, admin_headers, test_part):
    add_order(db_session, test_part, "LITERAL-UPPER", customer_po="PO%_\\Alpha")
    add_order(db_session, test_part, "LITERAL-LOWER", customer_po="po%_\\Beta")
    add_order(db_session, test_part, "WILDCARD-LOOKALIKE", customer_po="POXXAlpha")
    add_order(db_session, test_part, "OTHER-SLASH", customer_po="PO%_/Alpha")
    add_order(db_session, test_part, "CONTAINS-ONLY", customer_po="REF-PO%_\\Alpha")
    add_order(
        db_session,
        test_part,
        "NAME-ONLY",
        customer_name="PO%_\\Customer",
        customer_po="OTHER",
    )
    db_session.commit()

    result = suggestions(client, admin_headers, search="po%_\\")
    assert {item["customer_po"] for item in result["items"]} == {
        "PO%_\\Alpha",
        "po%_\\Beta",
    }
    assert all(item["customer_name"] == "Acme" for item in result["items"])
    assert result["has_more"] is False


def test_customer_filter_is_exact_and_omitted_differs_from_blank(client, db_session, admin_headers, test_part):
    for index, name in enumerate(["Acme", "acme", "Acme Aerospace", "Beta", None, ""]):
        add_order(db_session, test_part, f"CUSTOMER-{index}", customer_name=name)
    add_order(db_session, test_part, "DUPLICATE-PAIR")
    db_session.commit()

    all_customers = suggestions(client, admin_headers, search="PO-")
    assert len(all_customers["items"]) == 5
    assert {item["customer_name"] for item in all_customers["items"]} == {
        "Acme",
        "acme",
        "Acme Aerospace",
        "Beta",
        None,
    }
    assert suggestions(client, admin_headers, search="PO-", customer_name="Acme")["items"] == [
        {"customer_name": "Acme", "customer_po": "PO-100"}
    ]
    assert suggestions(client, admin_headers, search="PO-", customer_name="")["items"] == [
        {"customer_name": None, "customer_po": "PO-100"}
    ]
    assert suggestions(client, admin_headers, search="PO-", customer_name="Acm")["items"] == []


def test_includes_terminal_jobs_but_excludes_deleted_blank_and_other_tenant(
    client, db_session, admin_headers, test_part
):
    for status in WorkOrderStatus:
        add_order(
            db_session,
            test_part,
            f"STATUS-{status.value}",
            customer_po=f"PO-{status.value}",
            status=status,
        )
    add_order(db_session, test_part, "DELETED", customer_po="PO-DELETED", is_deleted=True)
    for index, value in enumerate([None, "", " \t\n\r\f\v"]):
        add_order(db_session, test_part, f"BLANK-{index}", customer_po=value)
    db_session.add(Company(id=2, name="Other tenant", slug="suggestion-other", is_active=True))
    db_session.flush()
    foreign_part = Part(
        company_id=2,
        part_number="FOREIGN",
        name="Foreign part",
        part_type="manufactured",
        unit_of_measure="each",
    )
    db_session.add(foreign_part)
    db_session.flush()
    add_order(db_session, foreign_part, "FOREIGN", customer_po="PO-SECRET")
    add_order(
        db_session,
        foreign_part,
        "FOREIGN-SAME-PO",
        customer_po="PO-draft",
        customer_name="Secret customer",
    )
    db_session.commit()

    result = suggestions(client, admin_headers, search="PO-")
    assert {item["customer_po"] for item in result["items"]} == {f"PO-{status.value}" for status in WorkOrderStatus}
    assert all(item["customer_name"] == "Acme" for item in result["items"])
    assert result["has_more"] is False
    assert suggestions(client, admin_headers, search="PO-SECRET")["items"] == []


@pytest.mark.parametrize("params", [{}, {"search": ""}, {"search": " \t\n"}])
def test_empty_search_does_not_list_all_purchase_orders(client, db_session, admin_headers, test_part, params):
    add_order(db_session, test_part, "PRESENT")
    db_session.commit()
    assert suggestions(client, admin_headers, **params) == {
        "items": [],
        "has_more": False,
    }


def test_limit_has_more_and_stable_unique_pairs(client, db_session, admin_headers, test_part):
    for index in [4, 1, 3, 0, 2]:
        for duplicate in range(2):
            add_order(
                db_session,
                test_part,
                f"ITEM-{index}-{duplicate}",
                customer_po=f"PO-{index}",
            )
    db_session.commit()

    limited = suggestions(client, admin_headers, search="PO-", limit=3)
    repeated = suggestions(client, admin_headers, search="PO-", limit=3)
    assert len(limited["items"]) == 3
    assert limited["has_more"] is True
    assert repeated == limited
    full = suggestions(client, admin_headers, search="PO-", limit=5)
    assert len(full["items"]) == 5
    assert len({(row["customer_name"], row["customer_po"]) for row in full["items"]}) == 5
    assert full["items"][:3] == limited["items"]
    assert full["has_more"] is False


@pytest.mark.parametrize("limit", [0, -1, 21])
def test_rejects_unbounded_limits(client, admin_headers, limit):
    assert client.get(URL, headers=admin_headers, params={"search": "PO-", "limit": limit}).status_code == 422


def test_suggestions_are_read_only_and_require_work_order_access(
    client, db_session, manager_headers, test_user, test_part
):
    add_order(db_session, test_part, "READ-ONLY", status=WorkOrderStatus.DRAFT)
    db_session.commit()
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", capture)
    try:
        assert suggestions(client, manager_headers, search="PO-")["items"] == [
            {"customer_name": "Acme", "customer_po": "PO-100"}
        ]
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture)
    assert not any(statement.lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")) for statement in statements)

    db_session.add(RolePermission(company_id=test_user.company_id, role=test_user.role, permissions=[]))
    db_session.commit()
    assert client.get(URL, headers=manager_headers, params={"search": "PO-"}).status_code == 403
    assert client.get(URL, params={"search": "PO-"}).status_code == 401
