"""Exclusion input bounds, exact persistence and legacy format compatibility."""

import copy
import hashlib
import math

import pytest
from pydantic import ValidationError

from app.models.audit_log import AuditLog
from app.models.quote_nesting_draft import QuoteNestingRevision
from app.schemas.quote_nesting_drafts import SavedProject
from app.services.quote_nesting_drafts import canonical_json
from tests.api.test_quote_nesting_drafts_contract import BASE, upload
from tests.services.test_quote_nesting_runs_service import estimate

pytestmark = pytest.mark.api


def exclusion(**changes):
    return {
        'id': 'reported-damage',
        'label': 'Synthetic surface damage',
        'reason': 'Estimator identified unavailable stock for this scenario',
        'outline': {'type': 'circle', 'cx': 8, 'cy': 4, 'r': 1},
        'clearance': 0.125,
        **changes,
    }


def exclusion_estimate():
    value = estimate()
    value['version'] = 12
    quote = value['groups'][0]['quote']
    quote['version'] = 11
    quote['options'][0]['exclusions'] = [exclusion()]
    return value


def test_exclusions_roundtrip_exact_imperial_hash_and_remain_unapproved(client, admin_headers, db_session):
    source = exclusion_estimate()
    response = upload(client, admin_headers, source)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['estimate'] == source and saved['payload_schema_version'] == 12
    assert saved['content_sha256'] == hashlib.sha256(canonical_json(source).encode()).hexdigest()
    assert saved['status'] == 'DRAFT'
    note = next(issue for issue in saved['review_issues'] if issue['code'] == 'unverified_stock_exclusions')
    assert note['group_id'] == 'g'
    assert 'not physically verified' in note['message'] and 'only during calculation' in note['message']
    reopened = client.get(f"{BASE}/{saved['draft_id']}/revisions/1", headers=admin_headers)
    assert reopened.json()['estimate'] == source
    assert db_session.query(QuoteNestingRevision).count() == 1
    audit = db_session.query(AuditLog).filter(AuditLog.resource_type == 'quote_nesting_revision').one()
    assert audit.new_values['content_sha256'] == saved['content_sha256']
    assert 'estimate' not in audit.new_values


@pytest.mark.parametrize('project_version,quote_version', [(4, 3), (5, 3), (6, 7), (10, 9)])
def test_legacy_absent_fields_keep_exact_saved_bytes_and_hash(client, admin_headers, project_version, quote_version):
    source = estimate()
    source['version'] = project_version
    source['groups'][0]['quote']['version'] = quote_version
    response = upload(client, admin_headers, source)
    assert response.status_code == 200, response.text
    assert response.json()['estimate'] == source
    assert response.json()['content_sha256'] == hashlib.sha256(canonical_json(source).encode()).hexdigest()
    assert all(issue['code'] != 'unverified_stock_exclusions' for issue in response.json()['review_issues'])


@pytest.mark.parametrize('quote_version', [3, 7, 9])
@pytest.mark.parametrize('regions', [[], [exclusion()]])
def test_old_quote_versions_cannot_silently_discard_even_an_empty_exclusion_field(quote_version, regions):
    source = exclusion_estimate()
    source['groups'][0]['quote']['version'] = quote_version
    source['groups'][0]['quote']['options'][0]['exclusions'] = regions
    with pytest.raises(ValidationError, match='quote version 11'):
        SavedProject.model_validate(source)


@pytest.mark.parametrize('project_version', [4, 5, 6, 10])
def test_exclusion_quote_cannot_be_relabelled_as_an_older_project(project_version):
    source = exclusion_estimate()
    source['version'] = project_version
    with pytest.raises(ValidationError, match='project version 12'):
        SavedProject.model_validate(source)


def test_project12_accepts_mixed_legacy_orientation_policy_and_exclusion_groups():
    source = exclusion_estimate()
    first = source['groups'][0]
    for index, version in enumerate((3, 7, 9)):
        group = copy.deepcopy(first)
        group['id'] = f'legacy-{version}'
        quote = group['quote']
        quote.update(version=version, thickness=(index + 3) / 16)
        quote['parts'][0]['id'] = f'legacy-part-{version}'
        quote['options'][0].pop('exclusions')
        if version == 7:
            quote['parts'][0].update(rotationMode='half-turn', grainAxis='x')
            quote['grainAxis'] = 'x'
        if version == 9:
            quote.update(
                spacingMode='manual',
                spacingOverride={
                    'schema_version': 1,
                    'reason': 'Synthetic review',
                    'changed_at': '2026-09-08T12:00:00Z',
                },
            )
        source['groups'].append(group)
    assert [group.quote.version for group in SavedProject.model_validate(source).groups] == [11, 3, 7, 9]
    quote = source['groups'][0]['quote']
    quote.update(spacingMode='manual', spacingOverride=copy.deepcopy(source['groups'][-1]['quote']['spacingOverride']))
    quote['parts'][0].update(rotationMode='fixed', grainAxis='y')
    quote['grainAxis'] = 'y'
    assert SavedProject.model_validate(source).groups[0].quote.parts[0].rotationMode == 'fixed'


@pytest.mark.parametrize(
    'regions',
    [
        None,
        {},
        [None],
        [exclusion(id='invalid id')],
        [exclusion(id='x' * 65)],
        [exclusion(label=' ')],
        [exclusion(label=' untrimmed')],
        [exclusion(label='x' * 121)],
        [exclusion(reason='')],
        [exclusion(reason='x' * 1001)],
        [exclusion(clearance=True)],
        [exclusion(clearance=-0.000000001)],
        [exclusion(clearance=100.000000001)],
        [exclusion(clearance=math.inf)],
        [exclusion(outline={'type': 'circle', 'cx': 2, 'cy': 2, 'r': 0})],
        [exclusion(outline={'type': 'circle', 'cx': 0.5, 'cy': 2, 'r': 1})],
        [exclusion(outline={'type': 'circle', 'cx': 12, 'cy': 2, 'r': 1})],
        [exclusion(outline={'type': 'poly', 'points': [{'x': 0, 'y': 0}, {'x': 1, 'y': 0}]})],
        [exclusion(outline={'type': 'poly', 'points': [{'x': 0, 'y': 0}, {'x': 13, 'y': 0}, {'x': 1, 'y': 1}]})],
        [exclusion(holes=[])],
        [exclusion(), exclusion()],
        [exclusion(id=f'region-{index}') for index in range(17)],
    ],
)
def test_malformed_or_outside_exclusions_are_rejected_before_any_history(regions, client, admin_headers, db_session):
    source = exclusion_estimate()
    source['groups'][0]['quote']['options'][0]['exclusions'] = regions
    response = upload(client, admin_headers, source)
    assert response.status_code == 422, response.text
    assert db_session.query(QuoteNestingRevision).count() == 0
    assert db_session.query(AuditLog).count() == 0


def test_gross_boundary_contact_overlap_and_large_clearance_are_explicit_not_cropped():
    source = exclusion_estimate()
    option = source['groups'][0]['quote']['options'][0]
    option['exclusions'] = [exclusion(id=f'region-{index}', clearance=100) for index in range(16)]
    option['exclusions'][0]['outline'] = {'type': 'circle', 'cx': 1, 'cy': 1, 'r': 1}
    assert len(SavedProject.model_validate(source).groups[0].quote.options[0].exclusions) == 16
    option['width'] = 8
    with pytest.raises(ValidationError, match='gross sheet'):
        SavedProject.model_validate(source)


def test_total_budget_counts_every_option_including_disabled_and_part_geometry():
    points = [
        {'x': 2 + math.cos(index * math.tau / 2000), 'y': 2 + math.sin(index * math.tau / 2000)}
        for index in range(2000)
    ]
    source = exclusion_estimate()
    quote = source['groups'][0]['quote']
    original = quote['options'][0]
    original['exclusions'] = [exclusion(outline={'type': 'poly', 'points': points})]
    quote['options'] = [
        {**copy.deepcopy(original), 'id': f'option-{index}', 'enabled': index == 0} for index in range(10)
    ]
    # 20,000 exclusion vertices plus one part circle must exceed the whole input cap.
    with pytest.raises(ValidationError, match='20,000 geometry vertices'):
        SavedProject.model_validate(source)
    quote['options'][-1]['exclusions'][0]['outline']['points'].pop()
    SavedProject.model_validate(source)
    # A separate circle consumes one source vertex, even though Node later tessellates it.
    quote['options'][0]['exclusions'].append(exclusion(id='extra-circle'))
    with pytest.raises(ValidationError, match='2,000 source vertices'):
        SavedProject.model_validate(source)


def test_unapproved_save_defers_topology_to_shared_calculation_with_explicit_review_note(client, admin_headers):
    source = exclusion_estimate()
    source['groups'][0]['quote']['options'][0]['exclusions'][0]['outline'] = {
        'type': 'poly',
        'points': [{'x': 1, 'y': 1}, {'x': 3, 'y': 3}, {'x': 1, 'y': 3}, {'x': 3, 'y': 1}],
    }
    response = upload(client, admin_headers, source)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'DRAFT'
    assert any(issue['code'] == 'unverified_stock_exclusions' for issue in response.json()['review_issues'])
