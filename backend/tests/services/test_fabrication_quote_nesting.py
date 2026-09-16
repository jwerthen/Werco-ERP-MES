import json

import pytest

from app.fabrication_quote.nesting import nest_parts


def request(quantity=4, **part):
    return {
        "parts": [
            {
                "id": "P",
                "quantity": quantity,
                "material": "A36",
                "thickness_mm": 3,
                "width_mm": 40,
                "height_mm": 40,
                **part,
            }
        ],
        "stocks": [
            {
                "id": "S",
                "quantity": 1,
                "material": "A36",
                "thickness_mm": 3,
                "width_mm": 100,
                "height_mm": 100,
                "price": "12.34",
                "currency": "USD",
            }
        ],
        "spacing_mm": 2,
        "edge_margin_mm": 3,
    }


def test_repeated_parts_finite_stock_cost_and_reproducibility():
    result = nest_parts(request(5))
    assert result == nest_parts(request(5))
    assert len(result["placements"]) == 4
    assert result["unplaced"][0]["quantity"] == 1
    assert result["sheet_count"] == 1
    assert result["cost_by_currency"] == {"USD": "12.34"}
    assert result["validated"] and not result["optimal"]
    json.dumps(result, allow_nan=False)
    for p in result["placements"]:
        x, y, xx, yy = p["envelope_mm"]
        assert 3 <= x < xx <= 97 and 3 <= y < yy <= 97


def test_rotation_is_an_explicit_grain_constraint():
    payload = request(1, width_mm=90, height_mm=40)
    payload["stocks"][0].update(width_mm=50, height_mm=100)
    assert nest_parts(payload)["unplaced"]
    payload["parts"][0]["allowed_rotations"] = [90]
    result = nest_parts(payload)
    assert not result["unplaced"]
    assert result["placements"][0]["rotation_degrees"] == 90


def test_material_and_thickness_must_match():
    payload = request(1)
    payload["stocks"][0]["material"] = "304"
    assert not nest_parts(payload)["placements"]
    payload["stocks"][0].update(material="A36", thickness_mm=4)
    assert not nest_parts(payload)["placements"]


def test_concave_part_envelope_is_conservative_and_holes_not_reused():
    payload = request(2, outline=[[0, 0], [80, 0], [80, 20], [20, 20], [20, 80], [0, 80]])
    result = nest_parts(payload)
    assert len(result["placements"]) == 1
    assert result["unplaced"][0]["quantity"] == 1


def test_remnant_notch_and_stock_hole_do_not_accept_envelope():
    payload = request(1, width_mm=80, height_mm=80)
    payload["stocks"][0]["outline"] = [
        [0, 0],
        [100, 0],
        [100, 20],
        [20, 20],
        [20, 100],
        [0, 100],
    ]
    assert not nest_parts(payload)["placements"]
    payload["stocks"][0].pop("outline")
    payload["stocks"][0]["holes"] = [[[5, 5], [95, 5], [95, 95], [5, 95]]]
    assert not nest_parts(payload)["placements"]


@pytest.mark.parametrize(
    "change",
    [
        {"quantity": -1},
        {"quantity": 1.5},
        {"quantity": 501},
        {"thickness_mm": float("nan")},
        {"allowed_rotations": [float("inf")]},
        {"units": "in"},
        {"mirror": True},
        {"outline": [[0, 0], [10, 10], [0, 10], [10, 0]]},
    ],
)
def test_bad_constraints_rejected(change):
    payload = request()
    payload["parts"][0].update(change)
    with pytest.raises(ValueError):
        nest_parts(payload)


def test_hole_equal_to_placement_envelope_is_not_stock():
    payload = request(1, width_mm=20, height_mm=20)
    payload.update(edge_margin_mm=0)
    payload["stocks"][0].update(
        width_mm=40,
        height_mm=40,
        outline=[[0, 0], [40, 0], [40, 40], [0, 40]],
        holes=[[[10, 10], [30, 10], [30, 30], [10, 30]]],
    )
    result = nest_parts(payload)
    for placed in result["placements"]:
        assert placed["envelope_mm"] != [10, 10, 30, 30]
