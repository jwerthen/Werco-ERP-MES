"""Independent negative oracles for the packaged recorded-piece smoke.

Geometry probes use the built local bundle when available. They do not claim the
Docker sandbox or production Node patch was exercised; the actual image smoke is
a separate required image-build step.
"""

import copy
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('remnant_image_smoke', ROOT / '.github/scripts/smoke_nesting_worker.py')
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)
pytestmark = [pytest.mark.unit]


@pytest.fixture(scope='module')
def actual_frames():
    bundle = ROOT / 'backend/nesting-runtime/solver.cjs'
    node = shutil.which('node')
    if not bundle.exists() or not node:
        pytest.skip('Build the shared Node worker before the local geometry smoke probes')
    manifest = json.loads(bundle.with_name('manifest.json').read_text())
    source = smoke.remnant_fixture()
    run = subprocess.run(
        [node, '--max-old-space-size=512', str(bundle)],
        input=json.dumps({'protocol': 2, 'input_sha256': 'a' * 64, 'estimate': source}),
        text=True,
        capture_output=True,
        timeout=25,
        check=False,
    )
    assert run.returncode == 0 and not run.stderr
    messages = [json.loads(line) for line in run.stdout.splitlines()]
    assert messages[0]['solver_version'] == 'werco-contour-v7'
    assert messages[0]['bundle_sha256'] == manifest['bundle_sha256']
    assert messages[0]['node_version'].startswith('v22.')
    # Only the independent geometry oracle is under test here. Package patch
    # pinning remains strict in smoke(); a host may use another supported22 patch.
    messages[0]['node_version'] = smoke.NODE_VERSION
    return source, manifest, messages


def test_actual_bundle_satisfies_the_independent_l_piece_oracle(actual_frames):
    source, manifest, messages = actual_frames
    smoke.verify_remnant_messages(messages, source, manifest)


@pytest.mark.parametrize(
    'mutation',
    [
        'outer',
        'hole',
        'overlap',
        'missing_corner',
        'physical_hole',
        'piece_count',
        'map',
        'original_id',
        'credit',
        'summary',
    ],
)
def test_recorded_smoke_rejects_false_geometry_or_partition(actual_frames, mutation):
    source, manifest, original = actual_frames
    messages = copy.deepcopy(original)
    piece, residual = messages[2:4]
    placement = piece['result']['nest']['placements'][0]
    if mutation == 'outer':
        piece['stock']['domain']['outer']['points'][2]['x'] += 1
    elif mutation == 'hole':
        piece['stock']['domain']['holes'][0]['points'][0]['x'] += 1
    elif mutation == 'overlap':
        other = piece['result']['nest']['placements'][1]
        other.update(x=placement['x'], y=placement['y'])
    elif mutation == 'missing_corner':
        placement.update(x=7 * 25.4, y=4 * 25.4)
    elif mutation == 'physical_hole':
        placement.update(x=25.4, y=25.4)
    elif mutation == 'piece_count':
        piece['result']['nest']['sheets'] = 2
    elif mutation == 'map':
        residual['instance_map'][0]['originals'] = [0, 9]
    elif mutation == 'original_id':
        placement['instance'] = 10
    elif mutation == 'credit':
        piece['result']['leftovers']['creditUSD'] = 1
    else:
        messages[-1]['complete_option_count'] = 3
    with pytest.raises(AssertionError):
        smoke.verify_remnant_messages(messages, source, manifest)


@pytest.mark.parametrize(
    'mutation', ['v6', 'missing_protocol2', 'base_profile', 'domain_profile', 'wrapper', 'root', 'patch']
)
def test_packaged_manifest_gate_refuses_drift_before_running_geometry(monkeypatch, mutation):
    base = smoke.profile_wrapper()
    domain = smoke.profile_wrapper('werco-remnant-domain-v1')
    manifest = {
        'protocol': 1,
        'node_major': 22,
        'solver_version': 'werco-contour-v7',
        'supported_protocols': [1, 2],
        'geometry_profile': base['identity'],
        'remnant_domain_profile': domain['identity'],
        'entrypoint': 'solver.cjs',
        'max_option_evaluations': 36,
        'bundle_sha256': 'b' * 64,
    }
    identity = copy.deepcopy(
        {
            'manifest': manifest,
            'profile': base,
            'remnant': domain,
            'node_version': smoke.NODE_VERSION,
            'uid': 1000,
            'actual_sha256': 'b' * 64,
        }
    )
    if mutation == 'v6':
        identity['manifest']['solver_version'] = 'werco-contour-v6'
    elif mutation == 'missing_protocol2':
        identity['manifest']['supported_protocols'] = [1]
    elif mutation == 'base_profile':
        identity['manifest']['geometry_profile']['sha256'] = '0' * 64
    elif mutation == 'domain_profile':
        identity['manifest']['remnant_domain_profile']['sha256'] = '0' * 64
    elif mutation == 'wrapper':
        identity['remnant']['profile']['rules']['capacity'] = 'unlimited'
    elif mutation == 'root':
        identity['uid'] = 0
    else:
        identity['node_version'] = 'v22.0.0'
    calls = []

    def inspect_only(*args):
        calls.append(args)
        return subprocess.CompletedProcess([], 0, json.dumps(identity), '')

    monkeypatch.setattr(smoke, 'container', inspect_only)
    with pytest.raises(AssertionError):
        smoke.smoke('synthetic-image')
    assert len(calls) == 1
