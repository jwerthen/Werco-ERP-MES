"""Independent profile digest and current worker evidence downgrade guards."""

import copy
import hashlib
import json
import math

import pytest

from app.core.nesting_geometry_profile import (
    PROFILE_PATH,
    checked_profile,
    geometry_profile_identity,
    geometry_profile_payload,
)
from app.schemas.quote_nesting_runs import SOLVER_VERSION
from app.services.nesting_run_protocol import RunProtocolError, validate_hello, validate_option
from tests.services.test_nesting_exclusions_protocol import DIGEST, checkpoint

pytestmark = pytest.mark.unit


def test_canonical_ascii_profile_identity_matches_the_cross_language_golden_digest():
    wrapper = json.loads(PROFILE_PATH.read_bytes())
    payload = {'id': wrapper['identity']['id'], 'profile': wrapper['profile']}
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    assert hashlib.sha256(canonical).hexdigest() == '21e8689fb2ce80c72befbc5866f658cd74fe8ed336d1b5c070e182f3081aa55a'
    assert checked_profile(json.dumps(wrapper, indent=4).encode()) == wrapper
    assert checked_profile(json.dumps(wrapper, sort_keys=True, separators=(',', ':')).encode()) == wrapper
    identity = geometry_profile_identity()
    identity['sha256'] = 'a' * 64
    profile = geometry_profile_payload()
    profile['numerics']['partGapFraction'] = '0'
    assert geometry_profile_identity() == wrapper['identity']
    assert geometry_profile_payload() == wrapper['profile']


@pytest.mark.parametrize(
    'change', ['id', 'digest', 'clearance', 'float', 'unicode', 'bool', 'extra', 'duplicate', 'large']
)
def test_packaged_profile_corruption_cannot_become_the_current_identity(change):
    wrapper = json.loads(PROFILE_PATH.read_bytes())
    if change == 'id':
        wrapper['identity']['id'] = 'other-profile'
    elif change == 'digest':
        wrapper['identity']['sha256'] = 'a' * 64
    elif change == 'clearance':
        wrapper['profile']['numerics']['numericalProtectionMm'] = '0'
    elif change == 'float':
        wrapper['profile']['numerics']['partGapFraction'] = 0.5
    elif change == 'unicode':
        wrapper['profile']['rules']['offsetJoin'] = '\N{GREEK SMALL LETTER ALPHA}'
    elif change == 'bool':
        wrapper['profile']['budgets']['maxCircleVertices'] = True
    elif change == 'extra':
        wrapper['approved'] = True
    raw = json.dumps(wrapper).encode()
    if change == 'duplicate':
        raw = raw.replace(b'"identity":', b'"identity":{},"identity":', 1)
    if change == 'large':
        raw += b' ' * 16385
    with pytest.raises(ValueError):
        checked_profile(raw)


@pytest.mark.parametrize('kind', ['absent', 'null', 'wrong-id', 'wrong-hash', 'extra'])
def test_hello_and_stock_must_bind_the_exact_source_geometry_identity(kind):
    identity = geometry_profile_identity()
    if kind == 'null':
        identity = None
    elif kind == 'wrong-id':
        identity['id'] = 'legacy'
    elif kind == 'wrong-hash':
        identity['sha256'] = 'a' * 64
    elif kind == 'extra':
        identity['approved'] = True
    hello = dict(
        type='hello',
        protocol=1,
        input_sha256=DIGEST,
        solver_version=SOLVER_VERSION,
        bundle_sha256='b' * 64,
        node_version='v22.23.2',
        units='mm',
        geometry_profile=identity,
    )
    manifest = dict(solver_version=SOLVER_VERSION, bundle_sha256='b' * 64, geometry_profile=geometry_profile_identity())
    message, expected = checkpoint(successful=True)
    message['stock']['geometryProfile'] = identity
    if kind == 'absent':
        hello.pop('geometry_profile')
        message['stock'].pop('geometryProfile')
    with pytest.raises(RunProtocolError):
        validate_hello(hello, DIGEST, manifest)
    with pytest.raises(RunProtocolError):
        validate_option(message, DIGEST, expected, 1)


@pytest.mark.parametrize('field', ['gap', 'margin', 'width', 'height', 'bedWidth', 'bedHeight'])
def test_current_stock_source_dimension_or_allowance_cannot_decrease_by_one_ulp(field):
    message, expected = checkpoint()
    validate_option(message, DIGEST, expected, 1)
    message['stock'][field] = math.nextafter(message['stock'][field], 0)
    with pytest.raises(RunProtocolError):
        validate_option(message, DIGEST, expected, 1)


@pytest.mark.parametrize('regions', [True, False, 'empty'])
@pytest.mark.parametrize('downgrade', ['version1', 'version2', 'missing-area', 'numeric-payload', 'old-reservation'])
def test_v3_evidence_cannot_downgrade_its_rules_or_omit_excluded_area(regions, downgrade):
    message, expected = checkpoint(regions=regions, successful=True)
    validate_option(message, DIGEST, expected, 1)
    report = message['result']['leftovers']
    if downgrade.startswith('version'):
        report['version'] = 'werco-leftovers-v' + downgrade[-1]
    elif downgrade == 'missing-area':
        report['sheets'][0].pop('excludedArea')
    elif downgrade == 'numeric-payload':
        report['assumptions']['profile']['numerics']['partGapFraction'] = 0.5
    else:
        report['assumptions']['reservation'] = 'Earlier nominal edge semantics'
    with pytest.raises(RunProtocolError):
        validate_option(message, DIGEST, expected, 1)


def test_current_stock_cannot_make_a_legacy_source_into_a_new_calculation():
    message, expected = checkpoint()
    expected = copy.deepcopy(expected)
    expected['quote']['version'] = 11
    expected['quote'].pop('geometryProfile')
    with pytest.raises(RunProtocolError):
        validate_option(message, DIGEST, expected, 1)
