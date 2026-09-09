"""Checked engineering identity, separate from material or business approval.

Only the normative, packaged ASCII JSON defines these settings. Its canonical
digest covers the ID and complete payload, excluding the self-reported digest.
Historical estimate/report canonicalization is deliberately not reused here.
"""

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

PROFILE_PATH = Path(__file__).resolve().parents[1] / 'data/nesting_profiles/werco-compensated-v1.json'


def _unique(pairs: list[tuple[str, Any]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate geometry profile field')
        value[key] = item
    return value


def checked_profile(raw: bytes) -> dict:
    """Verify the bounded immutable deployment source, never client settings."""
    if len(raw) > 16384:
        raise ValueError('Geometry profile exceeds the configuration limit')
    source = json.loads(raw.decode('ascii'), object_pairs_hook=_unique)
    if not isinstance(source, dict) or set(source) != {'identity', 'profile'}:
        raise ValueError('Invalid geometry profile wrapper')
    identity, profile = source['identity'], source['profile']
    if (
        not isinstance(identity, dict)
        or set(identity) != {'id', 'sha256'}
        or identity['id'] != 'werco-compensated-v1'
        or not isinstance(identity['sha256'], str)
        or re.fullmatch(r'[a-f0-9]{64}', identity['sha256']) is None
        or not isinstance(profile, dict)
    ):
        raise ValueError('Invalid geometry profile identity')
    stack = [profile]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if any(not key.isascii() for key in value):
                raise ValueError('Geometry profile keys must be ASCII')
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
        elif type(value) is str:
            if not value.isascii():
                raise ValueError('Geometry profile values must be ASCII')
        elif type(value) is not int:
            raise ValueError('Geometry profile constants require decimal strings or integers')
    canonical = json.dumps(
        {'id': identity['id'], 'profile': profile}, sort_keys=True, separators=(',', ':'), ensure_ascii=True
    )
    if hashlib.sha256(canonical.encode('ascii')).hexdigest() != identity['sha256']:
        raise ValueError('Geometry profile digest does not match its configuration')
    return source


_PROFILE = checked_profile(PROFILE_PATH.read_bytes())


def geometry_profile_identity() -> dict[str, str]:
    """Return an owned copy; callers cannot change the registered identity."""
    return dict(_PROFILE['identity'])


def geometry_profile_payload() -> dict:
    """Return the exact string-valued normative payload for evidence binding."""
    return copy.deepcopy(_PROFILE['profile'])


def is_current_geometry_profile(value: Any) -> bool:
    return isinstance(value, dict) and value == _PROFILE['identity']
