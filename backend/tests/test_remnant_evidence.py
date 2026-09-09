"""Cross-language evidence vectors and non-JSON/reflexive encoding boundaries."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.remnant_domain_profile import checked_profile, remnant_profile_identity, remnant_profile_payload
from app.core.remnant_evidence import canonical_evidence, evidence_sha256, target_group_sha256

pytestmark = pytest.mark.unit
VECTORS = json.loads((Path(__file__).parent / 'fixtures/remnant_evidence_golden.json').read_text())


@pytest.mark.parametrize('case', VECTORS, ids=[case['name'] for case in VECTORS])
def test_golden_ascii_and_binary64_values(case):
    assert canonical_evidence(case['value']) == case['canonical']
    assert evidence_sha256(case['value']) == case['sha256']
    assert case['canonical'].isascii()


def test_number_encoding_is_typed_and_uses_binary64_not_decimal_printing():
    assert canonical_evidence(1) == 'werco-remnant-evidence-v1\nd3ff0000000000000'
    assert canonical_evidence(0) == canonical_evidence(-0.0)
    assert evidence_sha256('1') != evidence_sha256(1) != evidence_sha256(True)
    assert canonical_evidence('\x7f😀') == 'werco-remnant-evidence-v1\ns"\\u007f\\ud83d\\ude00"'


@pytest.mark.parametrize(
    'value', [float('nan'), float('inf'), -float('inf'), 9007199254740992, 1e100, Decimal('1'), (), {1: 'x'}, b'x']
)
def test_non_json_and_unsafe_numbers_refuse(value):
    with pytest.raises(ValueError):
        canonical_evidence(value)


def test_cycles_and_utf16_duplicate_object_keys_refuse():
    circular = []
    circular.append(circular)
    with pytest.raises(ValueError, match='structural budget'):
        canonical_evidence(circular)
    with pytest.raises(ValueError, match='distinct in UTF-16'):
        canonical_evidence({'😀': 1, '\ud83d\ude00': 2})


def test_target_hash_preserves_every_raw_quote_field_and_is_order_independent():
    quote = {'thickness': 0.125, 'parts': [{'id': 'A', 'quantity': 2}], 'material': 'Carbon steel'}
    first = target_group_sha256('group', 'A36', quote)
    assert first == target_group_sha256('group', 'A36', dict(reversed(list(quote.items()))))
    for change in ({**quote, 'thickness': 0.125000001}, {**quote, 'grainAxis': 'x'}, {**quote, 'parts': []}):
        assert first != target_group_sha256('group', 'A36', change)
    assert first != target_group_sha256('group', 'a36', quote)


def test_profile_file_integrity_and_owned_copies():
    from app.core.remnant_domain_profile import PROFILE_PATH

    original = PROFILE_PATH.read_bytes()
    value = checked_profile(original)
    assert value['identity'] == remnant_profile_identity()
    value['profile']['rules']['capacity'] = 'unlimited'
    with pytest.raises(ValueError, match='hash'):
        checked_profile(json.dumps(value).encode('ascii'))
    owned = remnant_profile_payload()
    owned['rules']['capacity'] = 'unlimited'
    assert remnant_profile_payload()['rules']['capacity'] == 'one-per-scenario'
