"""Checked packaged remnant-domain identity, separate from existing geometry hashes."""

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app.core.nesting_geometry_profile import geometry_profile_identity

PROFILE_PATH = Path(__file__).resolve().parents[1] / 'data/nesting_profiles/werco-remnant-domain-v1.json'


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate remnant profile field')
        result[key] = value
    return result


def checked_profile(raw: bytes) -> dict:
    if len(raw) > 16384:
        raise ValueError('Remnant profile exceeds its configuration budget')
    value = json.loads(raw.decode('ascii'), object_pairs_hook=_unique)
    if not isinstance(value, dict) or set(value) != {'identity', 'profile'}:
        raise ValueError('Invalid remnant profile wrapper')
    identity, profile = value['identity'], value['profile']
    if (
        not isinstance(identity, dict)
        or set(identity) != {'id', 'sha256'}
        or identity['id'] != 'werco-remnant-domain-v1'
        or not isinstance(identity['sha256'], str)
        or not re.fullmatch('[a-f0-9]{64}', identity['sha256'])
        or not isinstance(profile, dict)
        or profile.get('compensatedProfile') != geometry_profile_identity()
    ):
        raise ValueError('Invalid remnant profile identity')
    stack = [profile]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if any(not key.isascii() for key in item):
                raise ValueError('Remnant profile keys require ASCII')
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif type(item) is str:
            if not item.isascii():
                raise ValueError('Remnant profile strings require ASCII')
        elif type(item) is not int:
            raise ValueError('Remnant profile numerics require decimal strings or integers')
    canonical = json.dumps(
        {'id': identity['id'], 'profile': profile}, sort_keys=True, separators=(',', ':'), ensure_ascii=True
    )
    if hashlib.sha256(canonical.encode('ascii')).hexdigest() != identity['sha256']:
        raise ValueError('Remnant profile content hash does not match')
    return value


_PROFILE = checked_profile(PROFILE_PATH.read_bytes())


def remnant_profile_identity() -> dict[str, str]:
    return dict(_PROFILE['identity'])


def remnant_profile_payload() -> dict:
    return copy.deepcopy(_PROFILE['profile'])


def is_current_remnant_profile(value: Any) -> bool:
    return isinstance(value, dict) and value == _PROFILE['identity']
