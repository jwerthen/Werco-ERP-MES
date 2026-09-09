"""An installed manifest cannot announce a different profile as ready."""

import hashlib
import json

import pytest

from app.core.nesting_geometry_profile import geometry_profile_identity
from app.core.remnant_domain_profile import remnant_profile_identity
from app.jobs import quote_nesting_runs as jobs
from app.schemas.quote_nesting_runs import SOLVER_VERSION

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'change',
    [
        'missing',
        'null',
        'wrong-hash',
        'extra',
        'old-solver',
        'missing-capabilities',
        'wrong-remnant',
        'boolean-capability',
    ],
)
async def test_invalid_packaged_profile_fails_before_any_executable_launch(tmp_path, monkeypatch, change):
    node = tmp_path / 'node'
    bundle = tmp_path / 'solver.cjs'
    manifest_path = tmp_path / 'manifest.json'
    node.write_bytes(b'Synthetic executable must never run')
    bundle.write_bytes(b'Synthetic bundle for identity refusal')
    manifest = {
        'protocol': 1,
        'solver_version': SOLVER_VERSION,
        'bundle_sha256': hashlib.sha256(bundle.read_bytes()).hexdigest(),
        'node_major': 22,
        'max_option_evaluations': 36,
        'entrypoint': 'solver.cjs',
        'geometry_profile': geometry_profile_identity(),
        'supported_protocols': [1, 2],
        'remnant_domain_profile': remnant_profile_identity(),
    }
    if change == 'missing':
        manifest.pop('geometry_profile')
    elif change == 'null':
        manifest['geometry_profile'] = None
    elif change == 'wrong-hash':
        manifest['geometry_profile']['sha256'] = 'a' * 64
    elif change == 'extra':
        manifest['geometry_profile']['approved'] = True
    elif change == 'missing-capabilities':
        manifest.pop('supported_protocols')
    elif change == 'wrong-remnant':
        manifest['remnant_domain_profile']['sha256'] = '0' * 64
    elif change == 'boolean-capability':
        manifest['supported_protocols'] = [True, 2]
    else:
        manifest['solver_version'] = 'werco-contour-v5'
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(jobs, 'NODE_PATH', node)
    monkeypatch.setattr(jobs, 'BUNDLE_PATH', bundle)
    monkeypatch.setattr(jobs, 'MANIFEST_PATH', manifest_path)

    async def unexpected_launch(*_args, **_kwargs):
        pytest.fail('Invalid profile must be refused before launching any executable')

    monkeypatch.setattr(jobs.asyncio, 'create_subprocess_exec', unexpected_launch)
    with pytest.raises(ValueError, match='runtime_unavailable'):
        await jobs.verify_runtime()
