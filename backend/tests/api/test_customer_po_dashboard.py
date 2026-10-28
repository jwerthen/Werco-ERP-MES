from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event

from app.models.company import Company
from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.part import Part
from app.models.role_permission import RolePermission
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus

LIST_URL = "/api/v1/work-orders/customer-pos"
DASHBOARD_URL = "/api/v1/work-orders/customer-po-dashboard"


def add(db, part, number, **overrides):
    values = {
        "company_id": part.company_id,
        "part_id": part.id,
        "work_order_number": number,
        "customer_name": "Acme",
        "customer_po": "PO-100",
        "status": WorkOrderStatus.RELEASED,
        "priority": 3,
        "quantity_ordered": 10,
    }
    values.update(overrides)
    row = WorkOrder(**values)
    db.add(row)
    return row


def dashboard(client, headers, **params):
    query = {"customer_name": "Acme", "customer_po": "PO-100"}
    query.update(params)
    response = client.get(DASHBOARD_URL, headers=headers, params=query)
    assert response.status_code == 200, response.text
    return response.json()


def add_operation(db, work_order, work_center, sequence, status, *, company_id=None):
    operation = WorkOrderOperation(
        company_id=company_id or work_order.company_id,
        work_order_id=work_order.id,
        work_center_id=work_center.id,
        sequence=sequence,
        operation_number=str(sequence),
        name=f"Operation {sequence}",
        status=status,
    )
    db.add(operation)
    db.flush()
    return operation


def add_cancelled_nest(db, work_order, operation, *, company_id=None):
    company_id = company_id or work_order.company_id
    package = LaserNestPackage(
        company_id=company_id,
        child_work_order_id=work_order.id,
        package_name=f"Cancelled package {operation.id}",
    )
    db.add(package)
    db.flush()
    db.add(
        LaserNest(
            company_id=company_id,
            package_id=package.id,
            work_order_operation_id=operation.id,
            nest_name=f"Cancelled nest {operation.id}",
            is_deleted=True,
        )
    )


def test_exact_pair_match_literal_search_and_tenant_isolation(client, db_session, admin_headers, test_part):
    match = add(db_session, test_part, "MATCH", customer_po="PO%_100")
    add(db_session, test_part, "OTHER-CUSTOMER", customer_name="Beta", customer_po="PO%_100")
    add(db_session, test_part, "OTHER-NUMBER", customer_po="POXX100")
    add(db_session, test_part, "CASE-DIFF", customer_po="po%_100")
    add(db_session, test_part, "PREFIX", customer_po="PO%_100-extra")
    db_session.add(Company(id=2, name="Other tenant", slug="po-other", is_active=True))
    db_session.flush()
    other_part = Part(
        company_id=2,
        part_number="OTHER",
        name="Other part",
        part_type="manufactured",
        unit_of_measure="each",
    )
    db_session.add(other_part)
    db_session.flush()
    add(db_session, other_part, "OTHER-TENANT", customer_po="PO%_100")
    add(db_session, other_part, "OTHER-TENANT-ONLY", customer_po="SECRET-PO")
    db_session.commit()

    result = dashboard(client, admin_headers, customer_po="PO%_100")
    assert result["total"] == 1
    assert [row["id"] for row in result["items"]] == [match.id]
    assert result["stats"]["total_work_orders"] == 1
    listed = client.get(LIST_URL, headers=admin_headers, params={"search": "%_"}).json()
    assert listed["total"] == 4
    assert all("%_" in row["customer_po"] for row in listed["items"])
    assert all(row["stats"]["total_work_orders"] == 1 for row in listed["items"])
    secret = client.get(LIST_URL, headers=admin_headers, params={"search": "SECRET-PO"}).json()
    assert secret["total"] == 0


def test_blank_customer_is_one_group_and_does_not_include_named_customer(client, db_session, admin_headers, test_part):
    unnamed = add(db_session, test_part, "NULL-CUSTOMER", customer_name=None)
    empty = add(db_session, test_part, "EMPTY-CUSTOMER", customer_name="")
    add(db_session, test_part, "NAMED-CUSTOMER")
    add(db_session, test_part, "NO-PO", customer_po=None)
    add(db_session, test_part, "EMPTY-PO", customer_po="")
    add(db_session, test_part, "WHITESPACE-PO", customer_po=" \t\n\r\f\v")
    db_session.commit()

    result = dashboard(client, admin_headers, customer_name="")
    assert {row["id"] for row in result["items"]} == {unnamed.id, empty.id}
    assert result["total"] == 2
    listed = client.get(LIST_URL, headers=admin_headers).json()
    assert listed["total"] == 2
    unnamed_groups = [row for row in listed["items"] if not row["customer_name"]]
    assert len(unnamed_groups) == 1
    assert unnamed_groups[0]["stats"]["total_work_orders"] == 2


def test_all_statuses_deleted_exclusion_and_full_po_stats_across_pages(client, db_session, admin_headers, test_part):
    today = datetime.now(ZoneInfo("America/Chicago")).date()
    statuses = list(WorkOrderStatus)
    ids = set()
    for index, status in enumerate(statuses):
        row = add(
            db_session,
            test_part,
            f"STATUS-{index}",
            status=status,
            due_date=today if status == WorkOrderStatus.RELEASED else today - timedelta(days=1),
        )
        db_session.flush()
        ids.add(row.id)
    add(db_session, test_part, "DELETED", is_deleted=True, due_date=today - timedelta(days=1))
    db_session.commit()

    pages = [dashboard(client, admin_headers, skip=skip, limit=2) for skip in [0, 2, 4, 6]]
    assert [len(page["items"]) for page in pages] == [2, 2, 2, 1]
    assert {row["id"] for page in pages for row in page["items"]} == ids
    assert len([row for page in pages for row in page["items"]]) == len(ids)
    assert all(page["total"] == 7 for page in pages)
    assert pages[-1]["has_next"] is False
    expected_stats = {
        "total_work_orders": 7,
        "active_work_orders": 4,
        "completed_work_orders": 2,
        "cancelled_work_orders": 1,
        "in_progress_work_orders": 1,
        "on_hold_work_orders": 1,
        "overdue_work_orders": 3,
        "due_today_work_orders": 1,
        "completion_percent": pytest.approx(33.3, abs=0.1),
        "next_due_date": (today - timedelta(days=1)).isoformat(),
    }
    assert all(page["stats"] == expected_stats for page in pages)
    assert pages[0]["as_of_date"] == today.isoformat()
    for page in pages:
        for row in page["items"]:
            terminal = row["status"] in {"complete", "closed", "cancelled"}
            assert row["is_overdue"] == (not terminal and row["due_date"] < today.isoformat())
    listed = client.get(LIST_URL, headers=admin_headers).json()
    assert listed["items"][0]["stats"] == expected_stats


def test_must_ship_by_overrides_due_date_for_promises_and_ordering(client, db_session, admin_headers, test_part):
    today = datetime.now(ZoneInfo("America/Chicago")).date()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)
    late = add(db_session, test_part, "SHIP-EARLY", due_date=tomorrow, must_ship_by=yesterday)
    deferred = add(db_session, test_part, "SHIP-LATER", due_date=yesterday, must_ship_by=tomorrow)
    due = add(db_session, test_part, "SHIP-TODAY", due_date=tomorrow, must_ship_by=today)
    fallback = add(db_session, test_part, "DUE-FALLBACK", due_date=today)
    terminal = add(
        db_session,
        test_part,
        "FINISHED",
        status=WorkOrderStatus.COMPLETE,
        due_date=yesterday,
        must_ship_by=today - timedelta(days=10),
    )
    db_session.commit()

    result = dashboard(client, admin_headers)
    rows = {row["id"]: row for row in result["items"]}
    assert rows[late.id]["is_overdue"] is True
    assert rows[deferred.id]["is_overdue"] is False
    assert rows[terminal.id]["is_overdue"] is False
    assert result["stats"]["overdue_work_orders"] == 1
    assert result["stats"]["due_today_work_orders"] == 2
    assert result["stats"]["next_due_date"] == yesterday.isoformat()
    ordered_ids = [row["id"] for row in result["items"]]
    assert ordered_ids[0] == late.id
    assert set(ordered_ids[1:3]) == {due.id, fallback.id}
    assert ordered_ids[3:] == [deferred.id, terminal.id]
    listed = client.get(LIST_URL, headers=admin_headers).json()
    assert listed["items"][0]["stats"] == result["stats"]


def test_po_list_paginates_groups_without_partial_stats(client, db_session, admin_headers, test_part):
    for index in range(7):
        for job in range(3):
            add(db_session, test_part, f"GROUP-{index}-{job}", customer_po=f"PO-{index}")
    db_session.commit()
    responses = [client.get(LIST_URL, headers=admin_headers, params={"skip": skip, "limit": 3}) for skip in [0, 3, 6]]
    assert all(response.status_code == 200 for response in responses)
    pages = [response.json() for response in responses]
    assert [len(page["items"]) for page in pages] == [3, 3, 1]
    assert all(page["total"] == 7 for page in pages)
    pairs = [(row["customer_name"], row["customer_po"]) for page in pages for row in page["items"]]
    assert len(set(pairs)) == 7
    assert all(row["stats"]["total_work_orders"] == 3 for page in pages for row in page["items"])
    assert pages[-1]["has_next"] is False


def test_route_progress_active_operations_and_parent_link(
    client, db_session, admin_headers, test_part, test_work_center
):
    parent = add(
        db_session, test_part, "PARENT", status=WorkOrderStatus.IN_PROGRESS, quantity_complete=2, po_line_item="10"
    )
    db_session.flush()
    child = add(
        db_session,
        test_part,
        "CHILD",
        parent_work_order_id=parent.id,
        work_order_type="laser_cutting",
        quantity_ordered=3,
    )
    operation_ids = []
    for sequence, name, status, quantity in [
        (10, "Cut", OperationStatus.COMPLETE, 10),
        (20, "Weld", OperationStatus.IN_PROGRESS, 5),
        (30, "Inspect", OperationStatus.ON_HOLD, 0),
    ]:
        operation = WorkOrderOperation(
            company_id=test_part.company_id,
            work_order_id=parent.id,
            work_center_id=test_work_center.id,
            sequence=sequence,
            operation_number=str(sequence),
            name=name,
            status=status,
            quantity_complete=quantity,
        )
        db_session.add(operation)
        db_session.flush()
        if status != OperationStatus.COMPLETE:
            operation_ids.append(operation.id)
    db_session.commit()

    result = dashboard(client, admin_headers)
    rows = {row["id"]: row for row in result["items"]}
    assert rows[child.id]["parent_work_order_id"] == parent.id
    row = rows[parent.id]
    assert row["po_line_item"] == "10"
    assert row["quantity_complete"] == 2
    assert row["operations_complete"] == 1
    assert row["operation_count"] == 3
    assert row["operation_progress_percent"] == 50
    assert {operation["id"] for operation in row["current_operations"]} == set(operation_ids)
    assert all(operation["work_center_name"] == test_work_center.name for operation in row["current_operations"])


def test_read_does_not_reconcile_or_write_and_workspace_permission_is_enforced(
    client, db_session, manager_headers, test_user, test_part, test_work_center
):
    work_order = add(db_session, test_part, "READ-ONLY")
    db_session.flush()
    db_session.add(
        WorkOrderOperation(
            company_id=test_part.company_id,
            work_order_id=work_order.id,
            work_center_id=test_work_center.id,
            sequence=10,
            operation_number="10",
            name="Complete operation",
            status=OperationStatus.COMPLETE,
            quantity_complete=10,
        )
    )
    db_session.commit()
    statements = []

    def capture(_conn, _cursor, statement, *_):
        statements.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", capture)
    try:
        assert client.get(LIST_URL, headers=manager_headers).status_code == 200
        result = dashboard(client, manager_headers)
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture)
    assert not any(statement.lstrip().upper().startswith(("UPDATE ", "INSERT ", "DELETE ")) for statement in statements)
    assert result["items"][0]["status"] == "released"
    db_session.add(RolePermission(company_id=test_user.company_id, role=test_user.role, permissions=[]))
    db_session.commit()
    assert client.get(LIST_URL, headers=manager_headers).status_code == 403
    assert (
        client.get(
            DASHBOARD_URL, headers=manager_headers, params={"customer_name": "Acme", "customer_po": "PO-100"}
        ).status_code
        == 403
    )


def test_floor_operation_holds_count_once_and_cancelled_nests_do_not_hide_ready_work(
    client, db_session, admin_headers, test_part, test_work_center
):
    floor_held = add(db_session, test_part, "FLOOR-HELD", status=WorkOrderStatus.IN_PROGRESS)
    header_held = add(db_session, test_part, "HEADER-HELD", status=WorkOrderStatus.ON_HOLD)
    mixed = add(db_session, test_part, "MIXED-NESTS", work_order_type="laser_cutting")
    cancelled_only = add(db_session, test_part, "CANCELLED-NESTS", work_order_type="laser_cutting")
    db_session.flush()
    add_operation(db_session, floor_held, test_work_center, 10, OperationStatus.ON_HOLD)
    add_operation(db_session, floor_held, test_work_center, 20, OperationStatus.ON_HOLD)
    cancelled_op = add_operation(db_session, mixed, test_work_center, 10, OperationStatus.ON_HOLD)
    ready_op = add_operation(db_session, mixed, test_work_center, 20, OperationStatus.READY)
    add_cancelled_nest(db_session, mixed, cancelled_op)
    only_op = add_operation(db_session, cancelled_only, test_work_center, 10, OperationStatus.ON_HOLD)
    add_cancelled_nest(db_session, cancelled_only, only_op)
    for status in [WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED]:
        terminal = add(db_session, test_part, f"TERMINAL-{status.value}", status=status)
        db_session.flush()
        add_operation(db_session, terminal, test_work_center, 10, OperationStatus.ON_HOLD)
    db_session.commit()

    result = dashboard(client, admin_headers)
    assert result["stats"]["on_hold_work_orders"] == 2
    rows = {row["id"]: row for row in result["items"]}
    assert rows[header_held.id]["status"] == "on_hold"
    assert rows[floor_held.id]["status"] == "in_progress"
    assert [operation["id"] for operation in rows[mixed.id]["current_operations"]] == [ready_op.id]
    assert rows[cancelled_only.id]["current_operations"] == []
    for row in result["items"]:
        if row["status"] in {"complete", "closed", "cancelled"}:
            assert row["current_operations"] == []
    listed = client.get(LIST_URL, headers=admin_headers).json()
    assert listed["items"][0]["stats"]["on_hold_work_orders"] == 2


def test_other_tenant_operations_and_nest_tombstones_cannot_change_current_work_or_holds(
    client, db_session, admin_headers, test_part, test_work_center
):
    db_session.add(Company(id=2, name="Other tenant", slug="hold-other", is_active=True))
    foreign_operation_job = add(db_session, test_part, "FOREIGN-OPERATION")
    foreign_nest_job = add(db_session, test_part, "FOREIGN-NEST", work_order_type="laser_cutting")
    db_session.flush()
    # Simulate inconsistent references: every read must still use the row's tenant.
    add_operation(db_session, foreign_operation_job, test_work_center, 10, OperationStatus.ON_HOLD, company_id=2)
    ready_op = add_operation(db_session, foreign_operation_job, test_work_center, 20, OperationStatus.READY)
    held_op = add_operation(db_session, foreign_nest_job, test_work_center, 10, OperationStatus.ON_HOLD)
    add_cancelled_nest(db_session, foreign_nest_job, held_op, company_id=2)
    db_session.commit()

    result = dashboard(client, admin_headers)
    assert result["stats"]["on_hold_work_orders"] == 1
    rows = {row["id"]: row for row in result["items"]}
    assert [operation["id"] for operation in rows[foreign_operation_job.id]["current_operations"]] == [ready_op.id]
    assert [operation["id"] for operation in rows[foreign_nest_job.id]["current_operations"]] == [held_op.id]
    assert rows[foreign_nest_job.id]["operation_count"] == 1
    listed = client.get(LIST_URL, headers=admin_headers).json()
    assert listed["items"][0]["stats"]["on_hold_work_orders"] == 1


def test_laser_progress_queries_remain_bounded_across_a_page(db_session, test_part, test_work_center):
    from app.services.customer_po_service import customer_po_dashboard

    company_id = test_part.company_id
    for index in range(8):
        job = add(db_session, test_part, f"LASER-PAGE-{index}", work_order_type="laser_cutting")
        db_session.flush()
        for sequence in [10, 20, 30]:
            operation = add_operation(db_session, job, test_work_center, sequence, OperationStatus.READY)
            if sequence == 30:
                operation.status = OperationStatus.ON_HOLD
                add_cancelled_nest(db_session, job, operation)
    db_session.commit()

    def read_page(limit):
        db_session.expire_all()
        statements = []

        def capture(_conn, _cursor, statement, *_):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(db_session.bind, "before_cursor_execute", capture)
        try:
            result = customer_po_dashboard(
                db_session, company_id, customer_name="Acme", customer_po="PO-100", limit=limit
            )
        finally:
            event.remove(db_session.bind, "before_cursor_execute", capture)
        return result, len(statements)

    small, small_queries = read_page(1)
    page, page_queries = read_page(8)
    assert len(small["items"]) == 1
    assert len(page["items"]) == 8
    assert all(len(row.current_operations) == 2 for row in page["items"])
    assert page_queries <= small_queries + 1


@pytest.mark.parametrize("url", [LIST_URL, DASHBOARD_URL])
@pytest.mark.parametrize("params", [{"limit": 101}, {"limit": 0}, {"skip": -1}])
def test_rejects_unbounded_pagination(client, admin_headers, url, params):
    query = {"customer_name": "Acme", "customer_po": "PO-100", **params}
    assert client.get(url, headers=admin_headers, params=query).status_code == 422
