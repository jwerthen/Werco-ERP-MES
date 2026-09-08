"""Worker source identity and versioned accounting; geometry runs in the real kernel."""

import copy
import hashlib
import math

import pytest

from app.services.nesting_run_protocol import EXCLUSION_PROFILE, RunProtocolError, expected_options, validate_option
from app.services.quote_nesting_drafts import canonical_json
from tests.api.test_quote_nesting_exclusions_contract import exclusion, exclusion_estimate

pytestmark = pytest.mark.unit
DIGEST = 'a' * 64


def checkpoint(*, regions=True, successful=False):
    source = exclusion_estimate()
    quote = source['groups'][0]['quote']
    quote['parts'][0]['quantity'] = 1
    option = quote['options'][0]
    if regions is False:
        option.pop('exclusions')
    elif regions == 'empty':
        option['exclusions'] = []
    else:
        option['exclusions'].append(
            exclusion(
                id='polygon', outline={'type': 'poly', 'points': [{'x': 6, 'y': 1}, {'x': 7, 'y': 1}, {'x': 6, 'y': 2}]}
            )
        )
    metric = copy.deepcopy(option)
    for dimension in ('width', 'height'):
        metric[dimension] *= 25.4
    for region in metric.get('exclusions', []):
        region['clearance'] *= 25.4
        outline = region['outline']
        if outline['type'] == 'circle':
            for field in ('cx', 'cy', 'r'):
                outline[field] *= 25.4
        else:
            for point in outline['points']:
                for field in ('x', 'y'):
                    point[field] *= 25.4
    stock = {
        'width': metric['width'],
        'height': metric['height'],
        'bedWidth': metric['width'],
        'bedHeight': metric['height'],
        'margin': quote['margin'] * 25.4,
        'gap': quote['gap'] * 25.4,
        'maxSheets': 1,
    }
    if 'exclusions' in metric:
        stock['exclusions'] = copy.deepcopy(metric['exclusions'])
    message = {
        'type': 'option',
        'protocol': 1,
        'input_sha256': DIGEST,
        'sequence': 1,
        'group_id': 'g',
        'option_id': 's',
        'units': 'mm',
        'requested': 1,
        'stock': stock,
        'result': {
            'option': metric,
            'nest': None,
            'error': 'Synthetic unavailable calculation',
            'complete': False,
            'area': 0,
            'cost': None,
        },
    }
    if successful:
        gross = stock['width'] * stock['height']
        usable = (stock['width'] - 2 * stock['margin']) * (stock['height'] - 2 * stock['margin'])
        nominal = math.pi * 25.4**2
        excluded = 3000 if metric.get('exclusions') else 0
        sheet = {
            'sheet': 0,
            'grossArea': gross,
            'usableArea': usable,
            'edgeMarginArea': gross - usable,
            'nominalPartArea': nominal,
            'reservedCutoutArea': 0,
            'clearanceAndProtectionArea': 500,
            'remainingArea': usable - nominal - excluded - 500,
            'reconciliationResidualArea': 0,
            'regions': [],
        }
        if excluded:
            sheet['excludedArea'] = excluded
        message['result'].update(
            error=None,
            complete=True,
            area=gross,
            cost=option['price'],
            nest={
                'placements': [
                    {
                        'partId': 'p',
                        'instance': 0,
                        'x': 10,
                        'y': 10,
                        'width': 50.8,
                        'height': 50.8,
                        'rotation': 0,
                        'sheet': 0,
                    }
                ],
                'unplaced': [],
                'sheets': 1,
                'area': nominal,
                'utilization': nominal / gross * 100,
                'method': 'Synthetic protocol fixture',
            },
            leftovers={
                'inputSignature': 'synthetic-layout-binding',
                'version': 'werco-leftovers-v2' if excluded else 'werco-leftovers-v1',
                'status': 'potential_review_only',
                'creditUSD': 0,
                'assumptions': {
                    'profile': {'exclusions': copy.deepcopy(EXCLUSION_PROFILE)} if excluded else {},
                    'reservation': 'Synthetic',
                    'internalHolesReserved': True,
                    'boundsAreUsableRectangles': False,
                    'eligibilityVerified': False,
                },
                'sheets': [sheet],
            },
        )
    return message, expected_options(source)[0]


@pytest.mark.parametrize('regions', [True, False, 'empty'])
def test_exact_source_presence_and_imperial_to_metric_identity_survive_checkpoint(regions):
    message, expected = checkpoint(regions=regions)
    digest, length = validate_option(message, DIGEST, expected, 1)
    canonical = canonical_json(message).encode()
    assert digest == hashlib.sha256(canonical).hexdigest() and length == len(canonical)


@pytest.mark.parametrize('target', ['stock', 'option'])
@pytest.mark.parametrize(
    'alteration', ['missing', 'null', 'reordered', 'id', 'label', 'reason', 'clearance', 'circle', 'polygon', 'extra']
)
def test_neither_stock_nor_result_option_can_drop_or_mutate_saved_exclusions(target, alteration):
    message, expected = checkpoint()
    value = message['stock'] if target == 'stock' else message['result']['option']
    if alteration == 'missing':
        value.pop('exclusions')
    elif alteration == 'null':
        value['exclusions'] = None
    elif alteration == 'reordered':
        value['exclusions'].reverse()
    elif alteration in ('id', 'label', 'reason'):
        value['exclusions'][0][alteration] += '-changed'
    elif alteration == 'clearance':
        old = value['exclusions'][0]['clearance']
        value['exclusions'][0]['clearance'] = math.nextafter(old, 0)
    elif alteration == 'circle':
        old = value['exclusions'][0]['outline']['r']
        value['exclusions'][0]['outline']['r'] = math.nextafter(old, 0)
    elif alteration == 'polygon':
        value['exclusions'][1]['outline']['points'][0]['x'] += 0.000000001
    else:
        value['exclusions'][0]['outline']['ignored'] = True
    with pytest.raises(RunProtocolError):
        validate_option(message, DIGEST, expected, 1)


@pytest.mark.parametrize('target', ['stock', 'option'])
def test_absent_and_explicit_empty_source_fields_are_not_interchangeable(target):
    message, expected = checkpoint(regions=False)
    (message['stock'] if target == 'stock' else message['result']['option'])['exclusions'] = []
    with pytest.raises(RunProtocolError):
        validate_option(message, DIGEST, expected, 1)


@pytest.mark.parametrize('regions', [True, False, 'empty'])
def test_nonempty_exclusions_alone_require_the_v2_zero_credit_ledger(regions):
    message, expected = checkpoint(regions=regions, successful=True)
    validate_option(message, DIGEST, expected, 1)
    report = message['result']['leftovers']
    report['version'] = 'werco-leftovers-v1' if regions is True else 'werco-leftovers-v2'
    with pytest.raises(RunProtocolError):
        validate_option(message, DIGEST, expected, 1)


@pytest.mark.parametrize('alteration', ['missing', 'negative', 'nan', 'bool', 'outside', 'unbalanced', 'credit'])
def test_excluded_area_cannot_be_missing_invalid_double_counted_or_credited(alteration):
    message, expected = checkpoint(successful=True)
    report = message['result']['leftovers']
    sheet = report['sheets'][0]
    if alteration == 'missing':
        sheet.pop('excludedArea')
    elif alteration == 'negative':
        sheet['excludedArea'] = -1
    elif alteration == 'nan':
        sheet['excludedArea'] = math.nan
    elif alteration == 'bool':
        sheet['excludedArea'] = True
    elif alteration == 'outside':
        sheet['excludedArea'] = sheet['usableArea'] + 1
    elif alteration == 'unbalanced':
        sheet['excludedArea'] += 1
    else:
        report['creditUSD'] = 1
    with pytest.raises(RunProtocolError):
        validate_option(message, DIGEST, expected, 1)


def test_v1_ledger_cannot_gain_a_silent_exclusion_area_field():
    message, expected = checkpoint(regions=False, successful=True)
    message['result']['leftovers']['sheets'][0]['excludedArea'] = 0
    with pytest.raises(RunProtocolError):
        validate_option(message, DIGEST, expected, 1)


@pytest.mark.parametrize(
    'field,value', [('version', 'unapproved-new-profile'), ('numericalProtectionMm', 0), ('partGapFraction', False)]
)
def test_changed_exclusion_guard_profile_cannot_enter_checkpoint(field, value):
    message, expected = checkpoint(successful=True)
    message['result']['leftovers']['assumptions']['profile']['exclusions'][field] = value
    with pytest.raises(RunProtocolError):
        validate_option(message, DIGEST, expected, 1)
