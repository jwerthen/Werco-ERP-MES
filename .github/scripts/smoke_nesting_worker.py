"""Exercise the packaged solver as its non-root user, without a network or secrets."""

import argparse
import copy
import hashlib
import json
import math
import subprocess
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

NODE_VERSION = "v22.23.2"
RUNTIME = "/app/nesting-runtime/"


def profile_wrapper(profile_id='werco-compensated-v1'):
    source = Path(__file__).resolve().parents[2] / 'backend/app/data/nesting_profiles' / (profile_id + '.json')
    wrapper = json.loads(source.read_bytes())
    canonical = json.dumps(
        {'id': wrapper['identity']['id'], 'profile': wrapper['profile']},
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
    )
    assert hashlib.sha256(canonical.encode('ascii')).hexdigest() == wrapper['identity']['sha256']
    return wrapper


def container(image, args, payload=None):
    name = "werco-nesting-smoke-" + uuid4().hex
    command = [
        "docker",
        "run",
        "--rm",
        "--name=" + name,
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--memory=1g",
        "--pids-limit=64",
        "-i",
        "--entrypoint=/usr/local/bin/node",
        image,
        "--max-old-space-size=512",
        *args,
    ]
    try:
        return subprocess.run(command, input=payload, text=True, capture_output=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        # Killing the docker CLI alone can leave its container computing. Remove
        # only this invocation's randomly named container, never unrelated work.
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=10, check=False)
        raise


def fixture():
    return {
        "version": 15,
        "units": "in",
        "currency": "USD",
        "name": "Image smoke",
        "activeGroupId": "group",
        "groups": [
            {
                "id": "group",
                "quote": {
                    "version": 14,
                    'geometryProfile': profile_wrapper()['identity'],
                    "units": "in",
                    "currency": "USD",
                    "name": "Image smoke",
                    "material": "Carbon steel",
                    "thickness": 0.125,
                    "margin": 0.125,
                    "gap": 0.125,
                    "objective": "area",
                    "spacingMode": "manual",
                    "parts": [
                        {
                            "id": "plate",
                            "name": "Synthetic plate",
                            "quantity": 2,
                            "rotate": True,
                            "color": 0,
                            "loops": [
                                {
                                    "type": "poly",
                                    "points": [
                                        {"x": 0, "y": 0},
                                        {"x": 1, "y": 0},
                                        {"x": 1, "y": 2},
                                        {"x": 0, "y": 2},
                                    ],
                                }
                            ],
                        }
                    ],
                    "options": [{"id": "sheet", "enabled": True, "price": None, "width": 6, "height": 6}],
                },
            }
        ],
    }


def exclusion_fixture():
    value = fixture()
    quote = value['groups'][0]['quote']
    quote['options'][0]['exclusions'] = [
        {
            'id': 'synthetic-unavailable-strip',
            'label': 'Synthetic stock defect',
            'reason': 'Image smoke uses no physical stock or machine coordinates',
            'clearance': 0.125,
            'outline': {
                'type': 'poly',
                'points': [
                    {'x': 0, 'y': 0},
                    {'x': 2, 'y': 0},
                    {'x': 2, 'y': 6},
                    {'x': 0, 'y': 6},
                ],
            },
        }
    ]
    return value


def verify_exclusion_option(option, source):
    quote = source['groups'][0]['quote']
    region = quote['options'][0]['exclusions'][0]
    metric = {
        **region,
        'clearance': region['clearance'] * 25.4,
        'outline': {
            'type': 'poly',
            'points': [{key: point[key] * 25.4 for key in ('x', 'y')} for point in region['outline']['points']],
        },
    }
    assert option['stock']['exclusions'] == option['result']['option']['exclusions'] == [metric]
    result = option['result']
    assert result['complete'] and len(result['nest']['placements']) == 2
    assert result['nest']['unplaced'] == []
    # Independent axis-aligned distance oracle: every plate must lie right of
    # the strip plus entered clearance, half-gap and the two numerical guards.
    boundary = (2 + region['clearance'] + quote['gap'] / 2) * 25.4 + 0.0008
    assert all(placement['x'] >= boundary - 1e-7 for placement in result['nest']['placements'])
    report = result['leftovers']
    assert report['version'] == 'werco-leftovers-v3'
    assert report['status'] == 'potential_review_only' and report['creditUSD'] == 0
    assert report['assumptions']['profile'] == profile_wrapper()['profile']
    for sheet in report['sheets']:
        assert 10 * 25.4**2 < sheet['excludedArea'] < 13 * 25.4**2
        accounted = sum(
            sheet[key]
            for key in (
                'edgeMarginArea',
                'excludedArea',
                'nominalPartArea',
                'reservedCutoutArea',
                'clearanceAndProtectionArea',
                'remainingArea',
                'reconciliationResidualArea',
            )
        )
        assert math.isclose(accounted, sheet['grossArea'], abs_tol=1e-7, rel_tol=1e-12)
        assert all(region['classification'] == 'review' and region['creditUSD'] == 0 for region in sheet['regions'])


def verify_compensated_option(option, source):
    """Independent rectangle envelope bounds, without calling the shared solver."""
    quote = source['groups'][0]['quote']
    stock, result = option['stock'], option['result']
    assert stock['geometryProfile'] == quote['geometryProfile'] == profile_wrapper()['identity']
    edge = (quote['margin'] + quote['gap'] / 2) * 25.4 + 0.0008
    placed = result['nest']['placements']
    for part in placed:
        for axis, dimension in (('x', 'width'), ('y', 'height')):
            assert edge - 1e-7 <= part[axis]
            assert part[axis] + part[dimension] <= stock[dimension] - edge + 1e-7
    for index, a in enumerate(placed):
        for b in placed[index + 1 :]:
            if a['sheet'] != b['sheet']:
                continue
            distance = math.hypot(
                max(0, b['x'] - a['x'] - a['width'], a['x'] - b['x'] - b['width']),
                max(0, b['y'] - a['y'] - a['height'], a['y'] - b['y'] - b['height']),
            )
            assert distance >= quote['gap'] * 25.4 + 0.0008 - 1e-7
    report = result['leftovers']
    assert report['version'] == 'werco-leftovers-v3'
    assert report['assumptions']['profile'] == profile_wrapper()['profile']
    assert report['creditUSD'] == 0
    if not quote['options'][0].get('exclusions'):
        assert all(sheet['excludedArea'] == 0 for sheet in report['sheets'])


def remnant_fixture():
    """Only synthetic coordinates and identities; no original shop drawing bytes."""
    return json.loads((Path(__file__).resolve().parents[1] / 'fixtures/nesting-remnant-smoke.json').read_text())


def verify_remnant_messages(messages, source, manifest):
    """Independent count, rectangular-distance and actual-L-material oracles."""
    assert len(messages) == 5
    hello, baseline, piece, residual, summary = messages
    assert hello == {
        'type': 'hello',
        'protocol': 2,
        'input_sha256': 'a' * 64,
        'units': 'mm',
        'solver_version': manifest['solver_version'],
        'bundle_sha256': manifest['bundle_sha256'],
        'node_version': NODE_VERSION,
        'geometry_profile': manifest['geometry_profile'],
        'remnant_domain_profile': manifest['remnant_domain_profile'],
    }
    for index, (frame, kind) in enumerate(zip(messages[1:4], ['baseline', 'recorded_piece', 'residual']), 1):
        assert frame['type'] == 'stage' and frame['protocol'] == 2 and frame['input_sha256'] == 'a' * 64
        assert frame['sequence'] == index and frame['stage_id'] == f'stage-{index:02d}' and frame['stage_kind'] == kind
        assert frame['group_id'] == 'g1' and frame['units'] == 'mm'
    assert baseline['option_id'] == residual['option_id'] == 'sheet' and piece['option_id'] is None
    assert baseline['depends_on'] is piece['depends_on'] is None and residual['depends_on'] == 'stage-02'
    assert baseline['instance_map'] is piece['instance_map'] is None
    assert baseline['requested'] == piece['requested'] == 10 and baseline['result']['nest']['sheets'] == 2
    verify_compensated_option(baseline, source)
    stock, result = piece['stock'], piece['result']
    assert (
        stock['maxSheets'] == result['nest']['sheets'] == 1
        and stock['domain']['profile'] == manifest['remnant_domain_profile']
    )
    assert stock['domain']['sourceOriginIn'] == {'x': '-0.000000001', 'y': '0'}
    shape = source['remnantPlan']['snapshot']['evidence']['geometry']
    origin = {'x': -1, 'y': 0}

    def metric(point):
        return {
            axis: (float(int(Decimal(point[axis]) * 1000000000) - origin[axis]) / 1000000000) * 25.4
            for axis in ('x', 'y')
        }

    assert stock['domain']['outer'] == {'type': 'poly', 'points': [metric(p) for p in shape['outer']]}
    assert stock['domain']['holes'] == [
        {'type': 'poly', 'points': [metric(p) for p in ring]} for ring in shape['holes']
    ]
    assert result['error'] is None and not result['complete'] and result['leftovers']['version'] == 'werco-leftovers-v4'
    assert 'cost' not in result and 'option' not in result
    placed = result['nest']['placements']
    assert len(placed) == 8 and all(p['sheet'] == 0 and p['partId'] == 'plate' for p in placed)
    clearance = (source['groups'][0]['quote']['margin'] + source['groups'][0]['quote']['gap'] / 2) * 25.4
    shift = 0.000000001 * 25.4
    for p in placed:
        assert math.isclose(p['width'], 50.8, abs_tol=1e-7) and math.isclose(p['height'], 50.8, abs_tol=1e-7)
        x0, y0, x1, y1 = p['x'], p['y'], p['x'] + p['width'], p['y'] + p['height']
        assert x0 >= clearance and y0 >= clearance
        assert x1 <= 12 * 25.4 + shift - clearance and y1 <= 8 * 25.4 - clearance
        # The absent upper-right quadrant is never stock. The measured 1x1 hole
        # also needs physical-edge clearance, even though it is inside extents.
        assert x1 <= 6 * 25.4 + shift - clearance or y1 <= 3 * 25.4 - clearance
        hole_distance = math.hypot(max(0, 25.4 + shift - x1, x0 - (50.8 + shift)), max(0, 25.4 - y1, y0 - 50.8))
        assert hole_distance >= clearance - 1e-7
    for index, a in enumerate(placed):
        for b in placed[index + 1 :]:
            distance = math.hypot(
                max(0, b['x'] - a['x'] - a['width'], a['x'] - b['x'] - b['width']),
                max(0, b['y'] - a['y'] - a['height'], a['y'] - b['y'] - b['height']),
            )
            assert distance >= source['groups'][0]['quote']['gap'] * 25.4 + 0.0008 - 1e-7
    originals = {p['instance'] for p in placed}
    assert len(originals) == len(placed)
    remaining = sorted(set(range(10)) - originals)
    assert residual['requested'] == 2 and residual['instance_map'] == [{'part_id': 'plate', 'originals': remaining}]
    assert residual['result']['complete'] and residual['result']['nest']['sheets'] == 1
    assert sorted(p['instance'] for p in residual['result']['nest']['placements']) == [0, 1]
    assert not residual['result']['nest']['unplaced']
    verify_compensated_option(residual, source)
    ledger = result['leftovers']
    assert ledger['status'] == 'potential_review_only' and ledger['creditUSD'] == 0
    assert ledger['assumptions']['profile']['remnantDomainProfile'] == manifest['remnant_domain_profile']
    for sheet in ledger['sheets']:
        assert math.isclose(sheet['grossArea'], (65 + 8e-9) * 25.4**2, rel_tol=1e-12, abs_tol=1e-7)
        assert sheet['grossArea'] >= sheet['protectedArea'] >= sheet['usableArea'] >= sheet['remainingArea']
        accounted = sum(
            sheet[k]
            for k in (
                'edgeMarginArea',
                'excludedArea',
                'nominalPartArea',
                'reservedCutoutArea',
                'clearanceAndProtectionArea',
                'remainingArea',
                'reconciliationResidualArea',
            )
        )
        assert math.isclose(accounted, sheet['grossArea'], rel_tol=1e-12, abs_tol=1e-7)
        assert all(r['classification'] == 'review' and r['creditUSD'] == 0 for r in sheet['regions'])
    assert summary == {
        'type': 'summary',
        'protocol': 2,
        'input_sha256': 'a' * 64,
        'evaluated_keys': [{'stage_id': f'stage-{i:02d}', 'group_id': 'g1'} for i in range(1, 4)],
        'evaluated_count': 3,
        'complete_option_count': 2,
        'total_options': 3,
        'stop_reason': 'completed',
    }


def smoke(image):
    inspect = container(
        image,
        [
            "-e",
            (
                'const fs=require("node:fs"),c=require("node:crypto"),p="' + RUNTIME + '";'
                'const manifest=JSON.parse(fs.readFileSync(p+"manifest.json"));'
                'const profile=JSON.parse(fs.readFileSync("/app/app/data/nesting_profiles/werco-compensated-v1.json"));'
                'const remnant=JSON.parse(fs.readFileSync("/app/app/data/nesting_profiles/werco-remnant-domain-v1.json"));'
                'console.log(JSON.stringify({manifest,profile,remnant,node_version:process.version,uid:process.getuid(),'
                'actual_sha256:c.createHash("sha256").update(fs.readFileSync(p+"solver.cjs")).digest("hex")}));'
            ),
        ],
    )
    assert inspect.returncode == 0 and not inspect.stderr, "Packaged Node runtime failed"
    identity = json.loads(inspect.stdout)
    manifest = identity["manifest"]
    assert identity["uid"] != 0, "The solver must run as the worker's non-root user"
    assert identity["node_version"] == NODE_VERSION
    assert manifest["protocol"] == 1 and manifest["node_major"] == 22
    assert manifest["solver_version"] == "werco-contour-v7"
    assert manifest['supported_protocols'] == [1, 2]
    assert identity['profile'] == profile_wrapper()
    assert manifest['geometry_profile'] == identity['profile']['identity']
    assert identity['remnant'] == profile_wrapper('werco-remnant-domain-v1')
    assert manifest['remnant_domain_profile'] == identity['remnant']['identity']
    assert manifest["entrypoint"] == "solver.cjs" and manifest["max_option_evaluations"] == 36
    assert manifest["bundle_sha256"] == identity["actual_sha256"]
    payload = {"protocol": 1, "input_sha256": "a" * 64, "estimate": fixture()}
    run = container(image, [RUNTIME + "solver.cjs"], json.dumps(payload))
    assert run.returncode == 0 and not run.stderr, "Packaged solver failed the synthetic geometry case"
    messages = [json.loads(line) for line in run.stdout.splitlines()]
    assert len(messages) == 3
    hello, option, summary = messages
    assert hello == {
        "type": "hello",
        "protocol": 1,
        "input_sha256": "a" * 64,
        "units": "mm",
        "solver_version": manifest["solver_version"],
        "bundle_sha256": manifest["bundle_sha256"],
        "node_version": NODE_VERSION,
        'geometry_profile': manifest['geometry_profile'],
    }
    assert option["type"] == "option" and option["requested"] == 2
    assert option["result"]["complete"] and len(option["result"]["nest"]["placements"]) == 2
    assert summary["type"] == "summary" and summary["stop_reason"] == "completed"
    assert summary["evaluated_count"] == summary["complete_option_count"] == summary["total_options"] == 1
    verify_compensated_option(option, payload['estimate'])
    excluded_source = exclusion_fixture()
    excluded_payload = {**payload, 'estimate': excluded_source}
    excluded = container(image, [RUNTIME + 'solver.cjs'], json.dumps(excluded_payload))
    assert excluded.returncode == 0 and not excluded.stderr, 'Packaged solver failed the stock exclusion case'
    excluded_messages = [json.loads(line) for line in excluded.stdout.splitlines()]
    assert [message['type'] for message in excluded_messages] == ['hello', 'option', 'summary']
    verify_exclusion_option(excluded_messages[1], excluded_source)
    verify_compensated_option(excluded_messages[1], excluded_source)
    assert excluded_messages[2]['complete_option_count'] == 1
    remnant_source = remnant_fixture()
    remnant_payload = {'protocol': 2, 'input_sha256': 'a' * 64, 'estimate': remnant_source}
    recorded = container(image, [RUNTIME + 'solver.cjs'], json.dumps(remnant_payload))
    assert recorded.returncode == 0 and not recorded.stderr, 'Packaged solver failed the actual recorded-piece case'
    verify_remnant_messages([json.loads(line) for line in recorded.stdout.splitlines()], remnant_source, manifest)
    excluded_source['version'] = 10
    excluded_source['groups'][0]['quote']['version'] = 9
    downgraded = container(image, [RUNTIME + 'solver.cjs'], json.dumps(excluded_payload))
    assert downgraded.returncode == 2 and not downgraded.stderr
    downgrade_messages = [json.loads(line) for line in downgraded.stdout.splitlines()]
    assert [message['type'] for message in downgrade_messages] == ['hello', 'error']
    assert downgrade_messages[-1]['code'] == 'invalid_geometry'
    # A valid old source remains openable/savable, but cannot be silently upgraded
    # by the worker. Preflight the entire mixed project before emitting an option.
    legacy_group = copy.deepcopy(fixture()['groups'][0])
    legacy_group['id'] = 'old-group'
    legacy_group['quote'].update(version=3, thickness=0.25)
    legacy_group['quote'].pop('geometryProfile')
    legacy_group['quote']['parts'][0]['id'] = 'old-plate'
    mixed = fixture()
    mixed['groups'].append(legacy_group)
    mixed_run = container(image, [RUNTIME + 'solver.cjs'], json.dumps({**payload, 'estimate': mixed}))
    assert mixed_run.returncode == 2 and not mixed_run.stderr
    mixed_messages = [json.loads(line) for line in mixed_run.stdout.splitlines()]
    assert [message['type'] for message in mixed_messages] == ['hello', 'error']
    assert mixed_messages[-1]['code'] == 'invalid_geometry'
    payload["estimate"]["units"] = "mm"
    invalid = container(image, [RUNTIME + "solver.cjs"], json.dumps(payload))
    assert invalid.returncode == 2 and not invalid.stderr
    rejected = [json.loads(line) for line in invalid.stdout.splitlines()]
    assert [message["type"] for message in rejected] == ["hello", "error"]
    assert rejected[-1]["code"] == "invalid_geometry"
    return {**manifest, "node_version": NODE_VERSION}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--manifest-out", type=Path, required=True)
    args = parser.parse_args()
    identity = smoke(args.image)
    args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
    args.manifest_out.write_text(json.dumps(identity, indent=2) + "\n")
    print("Non-root, network-disabled worker image smoke passed; bundle " + identity["bundle_sha256"])


if __name__ == "__main__":
    main()
