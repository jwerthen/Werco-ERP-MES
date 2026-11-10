from datetime import date, timedelta

import pytest
from sqlalchemy import event

from app.models.company import Company
from app.models.part import Part
from app.models.role_permission import RolePermission
from app.models.work_order import WorkOrder, WorkOrderStatus, WorkOrderType
from app.services.work_order_browse_service import browse_work_orders

URL = '/api/v1/work-orders/browse'


def add(db, part, number, **kw):
    row = WorkOrder(
        company_id=part.company_id,
        part_id=part.id,
        work_order_number=number,
        status=WorkOrderStatus.RELEASED,
        priority=3,
        quantity_ordered=10,
        **kw,
    )
    db.add(row)
    return row


def nest(db, company_id, number, **kw):
    """A standalone nest package: part-less, which the CHECK allows only for laser_cutting."""
    row = WorkOrder(
        company_id=company_id,
        part_id=None,
        work_order_number=number,
        work_order_type=WorkOrderType.LASER_CUTTING.value,
        status=WorkOrderStatus.RELEASED,
        priority=3,
        quantity_ordered=1,
        **kw,
    )
    db.add(row)
    return row


def test_bounded_stable_pages_exact_totals_and_group_counts(client, db_session, admin_headers, test_part):
    for i in range(123):
        add(
            db_session,
            test_part,
            f'BROWSE-{i:03}',
            customer_name='Acme' if i < 75 else 'Beta',
            due_date=date(2026, 1, 1),
        )
    db_session.commit()
    pages = [
        client.get(URL, headers=admin_headers, params={'skip': skip, 'limit': 50, 'group': 'customer'}).json()
        for skip in [0, 50, 100]
    ]
    assert [len(p['items']) for p in pages] == [50, 50, 23]
    assert all(p['total'] == 123 for p in pages)
    assert len({row['id'] for p in pages for row in p['items']}) == 123
    assert pages[0]['group_totals'] == {'Acme': 75}
    assert pages[1]['group_totals'] == {'Acme': 75, 'Beta': 48}
    assert pages[-1]['has_next'] is False
    filtered = client.get(
        URL,
        headers=admin_headers,
        params={'customer': 'Beta', 'sort': 'work_order_number', 'direction': 'desc', 'limit': 5},
    ).json()
    assert filtered['total'] == 48
    assert filtered['items'][0]['work_order_number'] == 'BROWSE-122'
    assert filtered['customers'] == ['Acme', 'Beta']


def test_filters_terminal_deleted_cots_scope_and_tenant(db_session, test_part):
    today = date(2026, 9, 7)
    late = add(db_session, test_part, 'LATE', customer_name='Acme', due_date=today - timedelta(days=1))
    due = add(db_session, test_part, 'DUE', due_date=today)
    complete = add(db_session, test_part, 'COMPLETE', due_date=today)
    complete.status = WorkOrderStatus.COMPLETE
    deleted = add(db_session, test_part, 'DELETED', due_date=today)
    deleted.is_deleted = True
    bought = Part(
        company_id=test_part.company_id,
        part_number='COTS',
        name='Bought part',
        part_type='purchased',
        unit_of_measure='each',
    )
    db_session.add(bought)
    db_session.flush()
    add(db_session, bought, 'BOUGHT', due_date=today)
    db_session.add(Company(id=2, name='Other', slug='browse-other', is_active=True))
    db_session.flush()
    other = Part(company_id=2, part_number='OTHER', name='Other part', part_type='manufactured', unit_of_measure='each')
    db_session.add(other)
    db_session.flush()
    add(db_session, other, 'OTHER', due_date=today)
    db_session.commit()
    result = browse_work_orders(db_session, test_part.company_id, today=today)
    assert {r.id for r in result['items']} == {late.id, due.id}
    assert result['stats'] == {'overdue': 1, 'in_progress': 0, 'due_today': 1}
    assert browse_work_orders(db_session, test_part.company_id, today=today, scope='overdue')['total'] == 1
    assert browse_work_orders(db_session, test_part.company_id, today=today, hide_cots=False)['total'] == 3
    assert (
        browse_work_orders(db_session, test_part.company_id, today=today, status=WorkOrderStatus.COMPLETE)['total'] == 1
    )


def test_literal_search_and_deleted_last_page_clamp(client, db_session, admin_headers, test_part):
    add(db_session, test_part, '100%_MATCH', unit_number='UNIT-LINK')
    add(db_session, test_part, '100OTHER')
    db_session.commit()
    assert client.get(URL, headers=admin_headers, params={'search': '%_'}).json()['total'] == 1
    response = client.get(URL, headers=admin_headers, params={'search': 'UNIT-LINK', 'skip': 100}).json()
    assert response['skip'] == 0 and response['total'] == 1


@pytest.mark.parametrize(
    'params',
    [
        {'limit': 101},
        {'sort': 'notes'},
        {'direction': 'random'},
        {'group': 'customer_name'},
        {'skip': -1},
        {'work_order_type': 'bogus'},
    ],
)
def test_reject_unbounded_or_unknown_query(client, admin_headers, params):
    assert client.get(URL, headers=admin_headers, params=params).status_code == 422


def test_permission_revocation_and_read_does_not_write(client, db_session, manager_headers, test_user, test_part):
    add(db_session, test_part, 'READ-ONLY')
    db_session.commit()
    statements = []

    def capture(_conn, _cursor, statement, *_):
        statements.append(statement)

    event.listen(db_session.bind, 'before_cursor_execute', capture)
    try:
        assert client.get(URL, headers=manager_headers).status_code == 200
    finally:
        event.remove(db_session.bind, 'before_cursor_execute', capture)
    assert not any(s.lstrip().upper().startswith(('UPDATE ', 'INSERT ', 'DELETE ')) for s in statements)
    db_session.add(RolePermission(company_id=test_user.company_id, role=test_user.role, permissions=[]))
    db_session.commit()
    assert client.get(URL, headers=manager_headers).status_code == 403


@pytest.mark.api
@pytest.mark.requires_db
def test_type_filter_narrows_rows_and_customers_but_never_its_own_counts(client, db_session, admin_headers, test_part):
    parent = add(db_session, test_part, 'PROD-PARENT', customer_name='Acme')
    add(db_session, test_part, 'PROD-BETA', customer_name='Beta')
    add(db_session, test_part, 'PROD-NOCUST')
    db_session.flush()
    nest(db_session, test_part.company_id, 'NEST-STANDALONE', customer_name='Gamma')
    add(
        db_session,
        test_part,
        'NEST-CHILD',
        customer_name='Acme',
        work_order_type=WorkOrderType.LASER_CUTTING.value,
        parent_work_order_id=parent.id,
    )
    nest(db_session, test_part.company_id, 'NEST-DELETED', customer_name='Deleted Co', is_deleted=True)
    db_session.add(Company(id=2, name='Other', slug='browse-type-other', is_active=True))
    db_session.flush()
    foreign = nest(db_session, 2, 'NEST-OTHER-TENANT', customer_name='Other Tenant Co')
    db_session.commit()

    def browse(**params):
        response = client.get(URL, headers=admin_headers, params=params)
        assert response.status_code == 200, response.text
        return response.json()

    nests = browse(work_order_type='laser_cutting')
    rows = {row['work_order_number']: row for row in nests['items']}
    assert set(rows) == {'NEST-STANDALONE', 'NEST-CHILD'} and nests['total'] == 2
    assert rows['NEST-STANDALONE']['part_id'] is None
    assert rows['NEST-CHILD']['part_id'] == test_part.id
    assert rows['NEST-CHILD']['parent_work_order_id'] == parent.id
    assert {row['work_order_type'] for row in nests['items']} == {'laser_cutting'}
    assert foreign.id not in {row['id'] for row in nests['items']}
    assert nests['customers'] == ['Acme', 'Gamma']
    assert browse(work_order_type='laser_cutting', group='part')['group_totals'] == {
        'Nest Packages': 1,
        test_part.part_number: 1,
    }
    production = browse(work_order_type='production')
    assert {row['work_order_number'] for row in production['items']} == {'PROD-PARENT', 'PROD-BETA', 'PROD-NOCUST'}
    assert production['total'] == 3 and production['customers'] == ['Acme', 'Beta']
    everything = browse()
    assert everything['total'] == 5 and everything['customers'] == ['Acme', 'Beta', 'Gamma']
    # The type filter is excluded from its own facet, so all three views agree; the
    # deleted nest and the other tenant's nest are counted by none of them.
    expected = {'production': 3, 'laser_cutting': 2}
    assert nests['type_counts'] == production['type_counts'] == everything['type_counts'] == expected


@pytest.mark.integration
@pytest.mark.requires_db
def test_type_counts_honor_every_other_filter_and_zero_fill(db_session, test_part):
    today = date(2026, 9, 7)
    company_id = test_part.company_id
    parent = add(db_session, test_part, 'P-ACME', customer_name='Acme')
    add(db_session, test_part, 'P-BETA', customer_name='Beta')
    add(db_session, test_part, 'P-DONE', customer_name='Acme').status = WorkOrderStatus.COMPLETE
    bought = Part(
        company_id=company_id, part_number='COTS-T', name='Bought part', part_type='purchased', unit_of_measure='each'
    )
    db_session.add(bought)
    db_session.flush()
    add(db_session, bought, 'P-COTS', customer_name='Acme')
    nest(db_session, company_id, 'N-ACME', customer_name='Acme', due_date=today - timedelta(days=1))
    add(
        db_session,
        test_part,
        'N-CHILD',
        customer_name='Beta',
        work_order_type=WorkOrderType.LASER_CUTTING.value,
        parent_work_order_id=parent.id,
    )
    nest(db_session, company_id, 'N-UNIT', unit_number='UNIT-TYPECOUNT')
    nest(db_session, company_id, 'N-DONE', customer_name='Acme').status = WorkOrderStatus.COMPLETE
    db_session.commit()

    def browse(**filters):
        return browse_work_orders(db_session, company_id, today=today, **filters)

    def counts(**filters):
        return browse(**filters)['type_counts']

    assert counts() == {'production': 2, 'laser_cutting': 3}
    assert counts(work_order_type=WorkOrderType.PRODUCTION) == {'production': 2, 'laser_cutting': 3}
    assert counts(work_order_type='laser_cutting') == {'production': 2, 'laser_cutting': 3}
    assert {row.work_order_number for row in browse(work_order_type=WorkOrderType.LASER_CUTTING)['items']} == {
        'N-ACME',
        'N-CHILD',
        'N-UNIT',
    }
    assert {row.work_order_number for row in browse(work_order_type='production')['items']} == {'P-ACME', 'P-BETA'}
    assert counts(hide_cots=False) == {'production': 3, 'laser_cutting': 3}
    assert counts(customer='Acme') == {'production': 1, 'laser_cutting': 1}
    beta_nests = browse(customer='Beta', work_order_type='laser_cutting')
    assert beta_nests['total'] == 1 and beta_nests['type_counts'] == {'production': 1, 'laser_cutting': 1}
    assert counts(status=WorkOrderStatus.COMPLETE) == {'production': 1, 'laser_cutting': 1}
    assert counts(search='typecount') == {'production': 0, 'laser_cutting': 1}
    assert counts(scope='overdue') == {'production': 0, 'laser_cutting': 1}
    assert counts(search='no-such-row') == {'production': 0, 'laser_cutting': 0}
