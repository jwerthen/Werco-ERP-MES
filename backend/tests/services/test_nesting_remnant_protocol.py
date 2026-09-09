"""Independent source conversion, instance conservation and protocol2 evidence gates."""

import copy
import hashlib
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.remnant_evidence import target_group_sha256
from app.db.database import atomic_transaction
from app.schemas.quote_nesting_drafts import SavedProject
from app.schemas.quote_nesting_runs import RunCheckpointResponse
from app.services import nesting_remnant_protocol as protocol
from app.services import quote_nesting_runs as runs
from app.services.audit_service import AuditService
from app.services.nesting_run_protocol import RunProtocolError
from app.services.quote_nesting_drafts import canonical_json, save_revision
from tests.api import test_remnant_planning as fixtures
from tests.api.test_remnant_planning import resolve, selection_for
from tests.services.test_quote_nesting_runs_service import (
    RUNTIME,
    current_estimate,
    request_for,
)

pytestmark = pytest.mark.integration
observed = fixtures.observed
stock = fixtures.stock
DIGEST = "e" * 64


@pytest.fixture
def project(client, admin_headers, observed):
    value = current_estimate()
    value.update(version=18, activeGroupId="group")
    value["groups"][0]["id"] = "group"
    selected, _ = selection_for(resolve(client, admin_headers, observed).json(), value["groups"][0]["quote"])
    value["remnantPlan"] = selected.model_dump(mode="json")
    return value


def recorded(project, instances=(0,)):
    stage = next(s for s in protocol.stage_plan(project) if s["stage_kind"] == "recorded_piece")
    quote = stage["quote"]
    stock = protocol.source_stock(project, quote)
    area = sum(protocol._part_areas(quote)["p"][0] for _ in instances)
    gross = protocol.loop_area(stock["domain"]["outer"])
    placements = [
        {
            "partId": "p",
            "instance": i,
            "x": 15 + 60 * n,
            "y": 15,
            "width": 50.8,
            "height": 50.8,
            "rotation": 0,
            "sheet": 0,
        }
        for n, i in enumerate(instances)
    ]
    remaining = 2 - len(instances)
    return {
        **{
            k: stage[k]
            for k in (
                "sequence",
                "stage_id",
                "stage_kind",
                "group_id",
                "option_id",
                "depends_on",
            )
        },
        "type": "stage",
        "protocol": 2,
        "input_sha256": DIGEST,
        "units": "mm",
        "requested": 2,
        "instance_map": None,
        "stock": stock,
        "result": {
            "error": None,
            "complete": not remaining,
            "area": gross if instances else 0,
            "nest": {
                "placements": placements,
                "unplaced": ([{"partId": "p", "count": remaining, "reason": "bounded search"}] if remaining else []),
                "sheets": int(bool(instances)),
                "area": area,
                "utilization": 100 * area / gross if instances else 0,
                "method": "synthetic_checked",
            },
        },
    }


def validate(frame, project, previous=()):
    return protocol.validate_stage(frame, DIGEST, project, frame["sequence"], list(previous))


def test_saved_raw_imperial_hash_is_preserved_and_legacy_discriminators_unchanged(
    project,
):
    parsed = SavedProject.model_validate(project)
    assert parsed.remnantPlan is not None
    assert parsed.remnantPlan.assignment.targetGroupSha256 == target_group_sha256(
        "group", "A36", project["groups"][0]["quote"]
    )
    assert protocol.selected_protocol(project) == 2
    no_plan = copy.deepcopy(project)
    no_plan.pop("remnantPlan")
    assert SavedProject.model_validate(no_plan).version == 18
    assert protocol.selected_protocol(no_plan) == 1
    for version in (4, 5, 6, 10, 12, 15):
        with pytest.raises(ValidationError):
            SavedProject.model_validate({**project, "version": version})
    with pytest.raises(ValidationError):
        SavedProject.model_validate({**project, "remnantPlan": None})


@pytest.mark.parametrize(
    "mutation",
    [
        "float_ulp",
        "grade",
        "snapshot_hash",
        "evidence_hash",
        "profile",
        "approval",
        "geometry",
    ],
)
def test_snapshot_and_full_group_binding_refuse_tampering(project, mutation):
    selected = project["remnantPlan"]
    if mutation == "float_ulp":
        project["groups"][0]["quote"]["gap"] = 0.12500000000000003
    elif mutation == "grade":
        selected["assignment"]["requiredGrade"] = "a36"
    elif mutation == "snapshot_hash":
        selected["snapshotSha256"] = "0" * 64
    elif mutation == "evidence_hash":
        selected["snapshot"]["payloadSha256"] = "0" * 64
    elif mutation == "profile":
        selected["geometryProfile"]["sha256"] = "0" * 64
    elif mutation == "approval":
        selected["eligibilityVerified"] = True
    else:
        selected["snapshot"]["evidence"]["geometry"]["width"] = "24.000000001"
    with pytest.raises(ValidationError):
        SavedProject.model_validate(project)


def test_shared_stage_budget_counts_all_baselines_and_each_residual(project):
    quote = project["groups"][0]["quote"]
    quote["options"] *= 1
    quote["options"] = [{**quote["options"][0], "id": f"s-{i}"} for i in range(12)]
    # 12 baselines +1piece +12 residual, plus12 baselines for a second group =37.
    other = copy.deepcopy(project["groups"][0])
    other["id"] = "other"
    other["quote"]["parts"][0]["id"] = "other-p"
    other["quote"]["thickness"] = 0.25
    project["groups"].append(other)
    project["remnantPlan"]["assignment"]["targetGroupSha256"] = target_group_sha256("group", "A36", quote)
    with pytest.raises(ValidationError, match="36-stage"):
        SavedProject.model_validate(project)
    with pytest.raises(RunProtocolError):
        protocol.stage_plan(project)


def test_source_stock_exact_nanoinch_origin_grain_and_zone_clearance(project):
    evidence = project["remnantPlan"]["snapshot"]["evidence"]
    evidence["geometry"] = {
        "kind": "circle",
        "cx": "-99999.999999999",
        "cy": "200",
        "r": "0.123456789",
    }
    evidence["grain_axis"] = "y"
    evidence["unavailable_zones"] = [
        {
            "id": "zone",
            "label": "Unverified",
            "reason": "Reported",
            "outline": {
                "kind": "circle",
                "cx": "-99999.999999999",
                "cy": "200",
                "r": "0.000000001",
            },
        }
    ]
    project["remnantPlan"]["zoneClearanceIn"] = "0.000000001"
    stock = protocol.source_stock(project, project["groups"][0]["quote"])
    assert stock["domain"]["sourceOriginIn"] == {
        "x": "-100000.123456788",
        "y": "199.876543211",
    }
    assert stock["domain"]["outer"]["cx"] == (123456789 / 1000000000) * 25.4
    assert stock["exclusions"][0]["clearance"] == (1 / 1000000000) * 25.4
    assert stock["grainAxis"] == "y" and stock["maxSheets"] == 1


def test_recorded_source_not_bbox_and_exact_original_instance_complement(project):
    frame = recorded(project, (1,))
    digest, size = validate(frame, project)
    encoded = canonical_json(frame).encode()
    assert digest == hashlib.sha256(encoded).hexdigest() and size == len(encoded)
    residual, mapping, count = protocol.derive_residual(project["groups"][0]["quote"], frame)
    assert mapping == [{"part_id": "p", "originals": [0]}] and count == 1
    expected = copy.deepcopy(project["groups"][0]["quote"])
    expected["parts"][0]["quantity"] = 1
    assert residual == expected
    assert project["groups"][0]["quote"]["parts"][0]["quantity"] == 2
    assert not protocol.completed_option(frame)


@pytest.mark.parametrize(
    "mutation",
    [
        "source_shape",
        "source_zone",
        "grain",
        "area_bbox",
        "double_instance",
        "missing_count",
        "unknown_part",
        "two_pieces",
        "financial",
        "false_complete",
        "null_stock",
        "wrong_stage",
        "wrong_protocol",
    ],
)
def test_recorded_counterfeit_or_incomplete_partition_rejected(project, mutation):
    frame = recorded(project)
    nest = frame["result"]["nest"]
    if mutation == "source_shape":
        frame["stock"]["domain"]["outer"]["points"][1]["x"] += 1e-9
    elif mutation == "source_zone":
        frame["stock"].pop("exclusions")
    elif mutation == "grain":
        frame["stock"]["grainAxis"] = "x"
    elif mutation == "area_bbox":
        frame["result"]["area"] += 1
    elif mutation == "double_instance":
        nest["placements"].append(copy.deepcopy(nest["placements"][0]))
    elif mutation == "missing_count":
        nest["unplaced"] = []
    elif mutation == "unknown_part":
        nest["unplaced"][0]["partId"] = "unknown"
    elif mutation == "two_pieces":
        nest["sheets"] = 2
    elif mutation == "financial":
        frame["result"]["cost"] = 0
    elif mutation == "false_complete":
        frame["result"]["complete"] = True
    elif mutation == "null_stock":
        frame["stock"] = None
    elif mutation == "wrong_stage":
        frame["stage_id"] = "stage-01"
    else:
        frame["protocol"] = True
    with pytest.raises(RunProtocolError):
        validate(frame, project)


def test_failed_recorded_stage_retains_all_instances_and_nullable_stock(project):
    frame = recorded(project)
    frame["stock"] = None
    frame["result"] = dict(nest=None, error="Source cannot be represented safely", complete=False, area=0)
    validate(frame, project)
    _, mapping, count = protocol.derive_residual(project["groups"][0]["quote"], frame)
    assert count == 2 and mapping == [{"part_id": "p", "originals": [0, 1]}]


def test_zero_residual_is_complete_option_but_recorded_stage_does_not_increment_count(
    project,
):
    predecessor = recorded(project, (0, 1))
    validate(predecessor, project)
    stage = protocol.stage_plan(project)[-1]
    message = {
        **{
            k: stage[k]
            for k in (
                "sequence",
                "stage_id",
                "stage_kind",
                "group_id",
                "option_id",
                "depends_on",
            )
        },
        "type": "stage",
        "protocol": 2,
        "input_sha256": DIGEST,
        "units": "mm",
        "requested": 0,
        "stock": None,
        "result": None,
        "instance_map": [],
    }
    validate(message, project, [predecessor])
    assert protocol.completed_option(message) and not protocol.completed_option(predecessor)
    message["instance_map"] = [{"part_id": "p", "originals": [1]}]
    with pytest.raises(RunProtocolError):
        validate(message, project, [predecessor])


def test_protocol_metadata_new_keys_do_not_appear_on_legacy_rows():
    common = dict(
        sequence=1,
        group_id="g",
        option_id="s",
        content_sha256=DIGEST,
        payload_bytes=10,
        created_at="2026-09-08T00:00:00Z",
        complete=True,
        sheets=1,
        placed=1,
        unplaced=0,
        result={},
    )
    legacy = RunCheckpointResponse(**common).model_dump()
    assert not {"stage_kind", "source_option_id", "depends_on"} & legacy.keys()
    stage = RunCheckpointResponse(**common, stage_kind="recorded_piece").model_dump()
    assert stage["stage_kind"] == "recorded_piece" and stage["source_option_id"] is None and stage["depends_on"] is None


def test_save_start_snapshot_currentness_and_queued_claim_historical_authority(
    project, db_session, admin_user, stock, monkeypatch
):
    from app.services import quote_nesting_run_outbox as outbox

    monkeypatch.setattr(outbox, "enqueue_job_best_effort", lambda *a, **k: True)
    with atomic_transaction(db_session):
        revision = save_revision(
            db_session,
            admin_user,
            1,
            AuditService(db_session, admin_user),
            content=json.dumps(project).encode(),
            request_key=str(uuid4()),
            expected_company_id=1,
        )
    with atomic_transaction(db_session):
        run = runs.start_run(
            db_session,
            admin_user,
            1,
            AuditService(db_session, admin_user),
            request_for(revision),
            RUNTIME,
        )
    assert run["settings"]["protocol"] == 2 and run["settings"]["runtime"]["protocol"] == 1
    # Future source drift blocks a NEW start/save, but cannot rewrite queued input.
    stock.quantity_on_hand = 9
    db_session.commit()
    with atomic_transaction(db_session):
        claim = runs.claim_run(db_session, 1, run["id"], runtime=RUNTIME)
    assert claim is not None and claim[0]["protocol"] == 2
    assert claim[0]["estimate"] == project
    with atomic_transaction(db_session):
        runs.finish_run(
            db_session,
            runs.get_run(db_session, 1, run["id"]),
            "user_cancelled",
            lease_token=claim[1],
        )
    with pytest.raises(HTTPException) as error, atomic_transaction(db_session):
        runs.start_run(
            db_session,
            admin_user,
            1,
            AuditService(db_session, admin_user),
            request_for(revision),
            RUNTIME,
        )
    assert error.value.status_code == 409


def test_ordinal_sequence_is_a_strict_integer_not_a_binary64_alias(project):
    frame = recorded(project)
    frame['sequence'] = float(frame['sequence'])
    with pytest.raises(RunProtocolError):
        protocol.validate_stage(frame, DIGEST, project, 2, [])


def test_original_instance_map_rejects_float_alias_before_result_validation(project):
    predecessor = recorded(project, (1,))
    stage = protocol.stage_plan(project)[-1]
    frame = {
        **{k: stage[k] for k in ('sequence', 'stage_id', 'stage_kind', 'group_id', 'option_id', 'depends_on')},
        'type': 'stage',
        'protocol': 2,
        'input_sha256': DIGEST,
        'units': 'mm',
        'requested': 1,
        'instance_map': [{'part_id': 'p', 'originals': [0.0]}],
        'stock': None,
        'result': None,
    }
    # A valid map proceeds to the ordinary-result validator; this malformed map
    # must be rejected before that boundary regardless of the result body.
    from unittest.mock import patch

    with patch.object(protocol, 'validate_option') as downstream:
        with pytest.raises(RunProtocolError):
            validate(frame, project, [predecessor])
        downstream.assert_not_called()
