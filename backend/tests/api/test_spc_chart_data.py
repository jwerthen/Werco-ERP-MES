"""GET /api/v1/spc/chart-data/{characteristic_id} — the bounded SPC chart read.

The endpoint now resolves the last-N DISTINCT subgroup_numbers via a
DISTINCT/ORDER BY DESC/LIMIT subquery and fetches only those subgroups' measurements
(a query shaped for ix_spc_measurements_char_subgroup), instead of materializing the
characteristic's full history; ``last_n_subgroups <= 0`` preserves the legacy
unbounded full-history path.

Covered here, none of which had a dedicated test before:

- window smaller than the data: exactly the last N subgroups, ascending order,
  correct per-subgroup mean/range/sample_count and the full top-level JSON shape;
- the last-N selection keys on subgroup_number (numerically highest), including
  non-contiguous, gap-numbered subgroups — not on row count or insertion order;
- ``last_n_subgroups=0`` returns the full history;
- window larger than the data (and exactly equal to it) returns everything;
- an empty dataset returns the established empty shape (empty chart_points,
  ``control_limits: null``, characteristic block intact) without error;
- per-subgroup OOC/violation aggregation and the ``is_current`` control-limit
  filter behave identically inside the bounded window;
- default window is 50; unknown characteristic is a 404.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.time_utils import to_utc_iso
from app.models.spc import SPCCharacteristic, SPCControlLimit, SPCMeasurement

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

CHART_DATA_URL = "/api/v1/spc/chart-data/{characteristic_id}"

BASE_TS = datetime(2026, 7, 1, 12, 0, 0)


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _make_characteristic(db: Session, part_id: int, **overrides) -> SPCCharacteristic:
    fields = {
        "company_id": 1,
        "name": "Bore diameter",
        "part_id": part_id,
        "characteristic_type": "dimensional",
        "unit_of_measure": "mm",
        "specification_nominal": 10.0,
        "specification_usl": 10.5,
        "specification_lsl": 9.5,
        "subgroup_size": 3,
    }
    fields.update(overrides)
    characteristic = SPCCharacteristic(**fields)
    db.add(characteristic)
    db.commit()
    db.refresh(characteristic)
    return characteristic


def _value(subgroup_number: int, sample_number: int) -> float:
    """Deterministic measurement value — the test recomputes means/ranges from this."""
    return 10.0 + (subgroup_number % 7) * 0.01 + sample_number * 0.001


def _seed_subgroups(
    db: Session,
    characteristic: SPCCharacteristic,
    subgroup_numbers,
    samples_per_subgroup: int = 3,
    *,
    ooc_subgroups=(),
    violation_rules_by_sample=None,
) -> None:
    """Seed samples_per_subgroup measurements for each subgroup number.

    ``violation_rules_by_sample`` maps (subgroup_number, sample_number) -> the raw
    ``violation_rules`` string, to exercise the split/strip/set aggregation.
    """
    violation_rules_by_sample = violation_rules_by_sample or {}
    for position, subgroup_number in enumerate(subgroup_numbers):
        for sample_number in range(1, samples_per_subgroup + 1):
            db.add(
                SPCMeasurement(
                    company_id=1,
                    characteristic_id=characteristic.id,
                    subgroup_number=subgroup_number,
                    sample_number=sample_number,
                    measurement_value=_value(subgroup_number, sample_number),
                    measured_at=BASE_TS + timedelta(minutes=position * 10 + sample_number),
                    is_out_of_control=subgroup_number in ooc_subgroups,
                    violation_rules=violation_rules_by_sample.get((subgroup_number, sample_number)),
                )
            )
    db.commit()


def _make_control_limit(db: Session, characteristic: SPCCharacteristic, *, is_current: bool = True, **overrides):
    fields = {
        "company_id": 1,
        "characteristic_id": characteristic.id,
        "ucl": 10.4,
        "lcl": 9.6,
        "center_line": 10.0,
        "ucl_range": 0.2,
        "lcl_range": 0.0,
        "center_line_range": 0.1,
        "sample_count": 25,
        "is_current": is_current,
    }
    fields.update(overrides)
    control_limit = SPCControlLimit(**fields)
    db.add(control_limit)
    db.commit()
    return control_limit


def _get_chart(client, headers, characteristic_id: int, **params):
    response = client.get(CHART_DATA_URL.format(characteristic_id=characteristic_id), headers=headers, params=params)
    return response


def _expected_point(subgroup_number: int, samples_per_subgroup: int) -> dict:
    """Recompute the mean/range oracle with the endpoint's own arithmetic."""
    values = [_value(subgroup_number, s) for s in range(1, samples_per_subgroup + 1)]
    return {
        "subgroup_number": subgroup_number,
        "mean": round(sum(values) / len(values), 6),
        "range": round(max(values) - min(values), 6) if len(values) > 1 else 0,
        "sample_count": len(values),
    }


# ---------------------------------------------------------------------------
# Windowing
# ---------------------------------------------------------------------------


def test_window_smaller_than_data_returns_exactly_the_last_n_subgroups(client, auth_headers, db_session, test_part):
    """12 subgroups, N=5 -> subgroups 8..12 in ascending order, values correct."""
    characteristic = _make_characteristic(db_session, test_part.id)
    _seed_subgroups(db_session, characteristic, range(1, 13), samples_per_subgroup=3)
    _make_control_limit(db_session, characteristic)

    response = _get_chart(client, auth_headers, characteristic.id, last_n_subgroups=5)
    assert response.status_code == 200, response.text
    data = response.json()

    # Top-level shape.
    assert set(data.keys()) == {"characteristic", "chart_points", "control_limits"}
    assert data["characteristic"]["id"] == characteristic.id
    assert data["characteristic"]["name"] == "Bore diameter"
    assert data["characteristic"]["chart_type"] == "xbar_r"
    assert data["characteristic"]["subgroup_size"] == 3
    assert data["characteristic"]["specification_nominal"] == 10.0
    assert data["characteristic"]["specification_usl"] == 10.5
    assert data["characteristic"]["specification_lsl"] == 9.5
    assert data["characteristic"]["unit_of_measure"] == "mm"

    # Exactly the last 5 subgroups, ascending.
    assert [p["subgroup_number"] for p in data["chart_points"]] == [8, 9, 10, 11, 12]

    # Per-point values recomputed independently.
    for point in data["chart_points"]:
        expected = _expected_point(point["subgroup_number"], samples_per_subgroup=3)
        assert point["mean"] == expected["mean"]
        assert point["range"] == expected["range"]
        assert point["sample_count"] == expected["sample_count"]
        assert point["is_out_of_control"] is False
        assert point["violations"] == []
        # measured_at is the subgroup's first sample, serialized UTC with a Z.
        assert point["measured_at"].endswith("Z")

    # The first sample of subgroup 8 (position 7 in the seed order) is the timestamp.
    expected_ts = to_utc_iso(BASE_TS + timedelta(minutes=7 * 10 + 1))
    assert data["chart_points"][0]["measured_at"] == expected_ts

    # Control limits come from the is_current row.
    assert data["control_limits"] == {
        "ucl": 10.4,
        "lcl": 9.6,
        "center_line": 10.0,
        "ucl_range": 0.2,
        "lcl_range": 0.0,
        "center_line_range": 0.1,
    }


def test_last_n_selects_by_subgroup_number_across_gap_numbered_subgroups(client, auth_headers, db_session, test_part):
    """Non-contiguous subgroup numbers: last-N means the N numerically highest."""
    characteristic = _make_characteristic(db_session, test_part.id)
    subgroup_numbers = [3, 7, 21, 22, 40, 41, 55, 90]  # deliberate gaps
    _seed_subgroups(db_session, characteristic, subgroup_numbers, samples_per_subgroup=2)

    response = _get_chart(client, auth_headers, characteristic.id, last_n_subgroups=3)
    assert response.status_code == 200, response.text
    points = response.json()["chart_points"]
    assert [p["subgroup_number"] for p in points] == [41, 55, 90]
    for point in points:
        expected = _expected_point(point["subgroup_number"], samples_per_subgroup=2)
        assert point["mean"] == expected["mean"]
        assert point["range"] == expected["range"]
        assert point["sample_count"] == 2


def test_zero_window_returns_the_full_unbounded_history(client, auth_headers, db_session, test_part):
    """last_n_subgroups=0 is the legacy full-history path."""
    characteristic = _make_characteristic(db_session, test_part.id)
    _seed_subgroups(db_session, characteristic, range(1, 61), samples_per_subgroup=1)

    response = _get_chart(client, auth_headers, characteristic.id, last_n_subgroups=0)
    assert response.status_code == 200, response.text
    points = response.json()["chart_points"]
    assert [p["subgroup_number"] for p in points] == list(range(1, 61))
    # Single-sample subgroups take the range=0 branch.
    assert all(p["range"] == 0 for p in points)
    assert all(p["sample_count"] == 1 for p in points)


def test_negative_window_preserves_the_legacy_quirk_byte_for_byte(client, auth_headers, db_session, test_part):
    """last_n_subgroups < 0 skips the bounded fetch AND keeps the legacy trim quirk.

    The pre-existing post-fetch trim ``sorted_sg_numbers[-last_n_subgroups:]`` slices
    ``[1:]`` for N=-1, dropping the FIRST subgroup — odd, but it is the legacy
    behavior the <= 0 path promises to preserve byte-for-byte, so this test pins it.
    If this ever fails because negative N was made to mean "full history", that is a
    deliberate behavior change to make explicitly, not an optimization side-effect.
    """
    characteristic = _make_characteristic(db_session, test_part.id)
    _seed_subgroups(db_session, characteristic, range(1, 8), samples_per_subgroup=1)

    response = _get_chart(client, auth_headers, characteristic.id, last_n_subgroups=-1)
    assert response.status_code == 200, response.text
    assert [p["subgroup_number"] for p in response.json()["chart_points"]] == list(range(2, 8))


def test_window_larger_than_data_returns_everything(client, auth_headers, db_session, test_part):
    characteristic = _make_characteristic(db_session, test_part.id)
    _seed_subgroups(db_session, characteristic, range(1, 5), samples_per_subgroup=2)

    response = _get_chart(client, auth_headers, characteristic.id, last_n_subgroups=50)
    assert response.status_code == 200, response.text
    points = response.json()["chart_points"]
    assert [p["subgroup_number"] for p in points] == [1, 2, 3, 4]
    for point in points:
        expected = _expected_point(point["subgroup_number"], samples_per_subgroup=2)
        assert point["mean"] == expected["mean"]
        assert point["range"] == expected["range"]


def test_window_exactly_equal_to_data_returns_everything(client, auth_headers, db_session, test_part):
    """Boundary: N == number of distinct subgroups drops nothing."""
    characteristic = _make_characteristic(db_session, test_part.id)
    _seed_subgroups(db_session, characteristic, range(1, 9), samples_per_subgroup=2)

    response = _get_chart(client, auth_headers, characteristic.id, last_n_subgroups=8)
    assert response.status_code == 200, response.text
    assert [p["subgroup_number"] for p in response.json()["chart_points"]] == list(range(1, 9))


def test_default_window_is_50_subgroups(client, auth_headers, db_session, test_part):
    """No query param: the documented default (last_n_subgroups=50) applies."""
    characteristic = _make_characteristic(db_session, test_part.id)
    _seed_subgroups(db_session, characteristic, range(1, 56), samples_per_subgroup=1)

    response = _get_chart(client, auth_headers, characteristic.id)
    assert response.status_code == 200, response.text
    points = response.json()["chart_points"]
    assert len(points) == 50
    assert [p["subgroup_number"] for p in points] == list(range(6, 56))


# ---------------------------------------------------------------------------
# Empty / missing datasets
# ---------------------------------------------------------------------------


def test_empty_dataset_returns_the_empty_shape_without_error(client, auth_headers, db_session, test_part):
    """No measurements, no control limits: 200 with the established empty shape."""
    characteristic = _make_characteristic(db_session, test_part.id)

    response = _get_chart(client, auth_headers, characteristic.id, last_n_subgroups=50)
    assert response.status_code == 200, response.text
    data = response.json()
    assert set(data.keys()) == {"characteristic", "chart_points", "control_limits"}
    assert data["chart_points"] == []
    assert data["control_limits"] is None
    assert data["characteristic"]["id"] == characteristic.id
    assert data["characteristic"]["chart_type"] == "xbar_r"


def test_empty_dataset_on_the_unbounded_path_matches(client, auth_headers, db_session, test_part):
    characteristic = _make_characteristic(db_session, test_part.id)

    response = _get_chart(client, auth_headers, characteristic.id, last_n_subgroups=0)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["chart_points"] == []
    assert data["control_limits"] is None


def test_unknown_characteristic_is_a_404(client, auth_headers, db_session):
    response = _get_chart(client, auth_headers, 999999, last_n_subgroups=50)
    assert response.status_code == 404
    assert response.json()["detail"] == "Characteristic not found"


# ---------------------------------------------------------------------------
# Aggregation inside the window
# ---------------------------------------------------------------------------


def test_ooc_and_violation_aggregation_inside_the_bounded_window(client, auth_headers, db_session, test_part):
    """OOC flag ORs across the subgroup; violation rules split/strip into a set."""
    characteristic = _make_characteristic(db_session, test_part.id)
    _seed_subgroups(
        db_session,
        characteristic,
        range(1, 7),
        samples_per_subgroup=2,
        ooc_subgroups={5},
        violation_rules_by_sample={
            (5, 1): "Rule1, Rule2",
            (5, 2): "Rule2",
        },
    )

    response = _get_chart(client, auth_headers, characteristic.id, last_n_subgroups=3)
    assert response.status_code == 200, response.text
    points = {p["subgroup_number"]: p for p in response.json()["chart_points"]}
    assert set(points.keys()) == {4, 5, 6}

    assert points[5]["is_out_of_control"] is True
    assert sorted(points[5]["violations"]) == ["Rule1", "Rule2"]
    assert points[4]["is_out_of_control"] is False
    assert points[4]["violations"] == []
    assert points[6]["is_out_of_control"] is False


def test_only_the_current_control_limit_is_served(client, auth_headers, db_session, test_part):
    """A superseded (is_current=False) limit row must not leak into the response."""
    characteristic = _make_characteristic(db_session, test_part.id)
    _seed_subgroups(db_session, characteristic, range(1, 4), samples_per_subgroup=2)
    _make_control_limit(db_session, characteristic, is_current=False, ucl=99.0, lcl=-99.0, center_line=0.0)
    _make_control_limit(db_session, characteristic, is_current=True)

    response = _get_chart(client, auth_headers, characteristic.id, last_n_subgroups=50)
    assert response.status_code == 200, response.text
    limits = response.json()["control_limits"]
    assert limits["ucl"] == 10.4
    assert limits["lcl"] == 9.6
    assert limits["center_line"] == 10.0


def test_bounded_and_unbounded_paths_agree_on_the_overlap(client, auth_headers, db_session, test_part):
    """The bounded read is an optimization, not a semantic change: for the same
    characteristic, the last-N points of the unbounded response must equal the
    bounded response's points exactly."""
    characteristic = _make_characteristic(db_session, test_part.id)
    _seed_subgroups(
        db_session,
        characteristic,
        range(1, 16),
        samples_per_subgroup=3,
        ooc_subgroups={13},
        violation_rules_by_sample={(13, 2): "Rule1"},
    )
    _make_control_limit(db_session, characteristic)

    bounded = _get_chart(client, auth_headers, characteristic.id, last_n_subgroups=6).json()
    unbounded = _get_chart(client, auth_headers, characteristic.id, last_n_subgroups=0).json()

    assert len(unbounded["chart_points"]) == 15
    assert bounded["chart_points"] == unbounded["chart_points"][-6:]
    assert bounded["characteristic"] == unbounded["characteristic"]
    assert bounded["control_limits"] == unbounded["control_limits"]


def test_chart_data_requires_authentication(client, db_session, test_part):
    characteristic = _make_characteristic(db_session, test_part.id)
    response = client.get(CHART_DATA_URL.format(characteristic_id=characteristic.id))
    assert response.status_code == 401
