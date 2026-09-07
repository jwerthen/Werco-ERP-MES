"""Search ranks before limits and never mistakes a capped sample for a total."""

import pytest
from sqlalchemy.dialects import postgresql

from app.models.part import Part
from app.models.part_number_alias import PartNumberAlias
from app.models.user import UserRole
from app.services.search_service import _search_statement

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


def add_part(db, number, **kwargs):
    part = Part(company_id=1, part_number=number, name="Search fixture", part_type="manufactured", **kwargs)
    db.add(part)
    db.flush()
    return part


def test_exact_and_retired_identifiers_survive_many_substring_hits(client, auth_headers, db_session, test_user):
    for index in range(60):
        add_part(db_session, f"TARGET-{index:03}")
    exact = add_part(db_session, "TARGET")
    retired = add_part(db_session, "RENAMED")
    db_session.add(
        PartNumberAlias(
            company_id=1,
            part_id=retired.id,
            alias_number="OLD-TARGET",
            alias_number_key="OLD-TARGET",
            reason="Renumber",
            created_by=test_user.id,
        )
    )
    db_session.commit()
    response = client.get('/api/v1/search/', headers=auth_headers, params={'q': 'TARGET', 'limit': 1}).json()
    assert response['results'][0]['id'] == exact.id
    assert response['total'] == 62
    assert response['has_more'] is True
    result = client.get('/api/v1/search/', headers=auth_headers, params={'q': 'OLD-TARGET', 'limit': 1}).json()
    assert result['results'][0]['id'] == retired.id
    assert result['results'][0]['matched_alias'] == 'OLD-TARGET'


def test_pages_are_stable_disjoint_and_report_full_count(client, auth_headers, db_session):
    for index in range(7):
        add_part(db_session, f"PAGED-{index}")
    db_session.commit()
    pages = [
        client.get(
            '/api/v1/search/',
            headers=auth_headers,
            params={'q': 'PAGED', 'types': 'part', 'limit': 3, 'offset': offset},
        ).json()
        for offset in (0, 3, 6)
    ]
    assert [page['total'] for page in pages] == [7, 7, 7]
    assert [page['has_more'] for page in pages] == [True, True, False]
    assert len({hit['id'] for page in pages for hit in page['results']}) == 7
    assert pages[0]['categories'] == {'part': 7}


def test_literal_wildcards_are_not_an_unbounded_search(client, auth_headers, db_session):
    add_part(db_session, 'LITERAL-%_')
    add_part(db_session, 'LITERAL-OTHER')
    db_session.commit()
    response = client.get('/api/v1/search/', headers=auth_headers, params={'q': '%_'}).json()
    assert response['total'] == 1
    assert response['results'][0]['title'] == 'LITERAL-%_'


def test_unknown_type_and_negative_offset_refused(client, auth_headers):
    for params in ({'q': 'x', 'types': 'invented'}, {'q': 'x', 'offset': -1}):
        assert client.get('/api/v1/search/', headers=auth_headers, params=params).status_code == 422


def test_search_union_compiles_for_postgres_with_correlated_alias_rank(test_user):
    test_user.role = UserRole.ADMIN
    statement = _search_statement(1, test_user, 'OLD-TARGET')
    sql = str(statement.select().compile(dialect=postgresql.dialect()))
    assert 'UNION ALL' in sql
    assert 'EXISTS' in sql
    assert 'part_number_aliases.company_id' in sql


@pytest.mark.parametrize('kind', ['bom', 'routing'])
def test_active_tombstoned_engineering_records_are_absent_from_hits_and_counts(client, auth_headers, db_session, kind):
    from app.models.bom import BOM
    from app.models.routing import Routing

    part = add_part(db_session, 'ENG-TOMBSTONE')
    model = BOM if kind == 'bom' else Routing
    db_session.add(model(company_id=1, part_id=part.id, revision='A', is_active=True, is_deleted=True))
    db_session.commit()
    response = client.get('/api/v1/search/', headers=auth_headers, params={'q': 'ENG-TOMBSTONE', 'types': kind})
    assert response.status_code == 200
    assert response.json()['results'] == []
    assert response.json()['total'] == 0
    assert response.json()['categories'] == {}


def test_empty_type_list_keeps_counts_consistent_with_results(client, auth_headers, db_session):
    add_part(db_session, 'EMPTY-TYPES')
    db_session.commit()
    result = client.get('/api/v1/search/', headers=auth_headers, params={'q': 'EMPTY-TYPES', 'types': ', '}).json()
    assert result['total'] == len(result['results']) == 1
    assert result['categories'] == {'part': 1}
