"""Protocol2 identity/accounting checks; the pinned Node kernel owns topology.

No available material is inferred from bounding boxes. Source stock is rebuilt
from immutable canonical inch strings, and residual instances derive solely from
an already accepted predecessor checkpoint in the same run.
"""

import hashlib
import math
from copy import deepcopy

from app.core.nesting_geometry_profile import geometry_profile_identity
from app.core.remnant_domain_profile import (
    remnant_profile_identity,
    remnant_profile_payload,
)
from app.core.remnant_evidence import canonical_evidence
from app.schemas.quote_nesting_runs import MAX_MESSAGE_BYTES, MAX_OPTIONS
from app.services.nesting_run_protocol import (
    _same_number,
    exact,
    expected_options,
    integer,
    number,
    require,
    validate_nest_structure,
    validate_option,
    validate_summary,
)
from app.services.quote_nesting_drafts import canonical_json

DOMAIN_RESERVATION = (
    "Compensated outer part envelopes are subtracted once from the actual inward-protected material after "
    "guarded unavailable zones; physical holes are absent from gross material, while part internal cutouts "
    "remain reserved. Boundary contact and original nominal clearances follow the recorded-piece domain "
    "profile. Potential review only; no physical eligibility, availability, reservation or credit."
)
DOMAIN_AREA_DEFINITIONS = {
    "grossArea": "Analytical reported outer area minus physical holes; bounding extents are not material.",
    "protectedArea": "Actual vector material after inward physical-edge margin and numerical protection, before unavailable zones.",
    "usableArea": "Actual protected material after the union of guarded unavailable zones.",
    "edgeMarginArea": "Gross minus protected area, including physical-edge margin and conservative numerical/curve protection.",
    "excludedArea": "Protected minus usable area; overlapping guarded unavailable zones are counted once.",
    "clearanceAndProtectionArea": "Usable minus remaining, nominal parts and reserved internal cutouts; part envelopes and numerical protection, not physical kerf.",
    "remainingArea": "Connected vector regions after compensated part-envelope subtraction; potential review only, no availability or credit.",
}


def selected_protocol(estimate: dict) -> int:
    return 2 if "remnantPlan" in estimate else 1


def same_source(actual, expected) -> None:
    # The dedicated representation distinguishes booleans from numbers while
    # preserving JSON integer/float equivalence and exact binary64 source values.
    require(canonical_evidence(actual) == canonical_evidence(expected))


def stage_plan(estimate: dict) -> list[dict]:
    require(estimate.get("version") == 18 and isinstance(estimate.get("remnantPlan"), dict))
    target_id = estimate["remnantPlan"]["groupId"]
    targets = [g for g in estimate["groups"] if g["id"] == target_id]
    require(len(targets) == 1 and targets[0]["quote"]["parts"])
    target = targets[0]["quote"]
    plan = []

    def add(kind, group_id, option_id, quote, option=None, depends_on=None):
        sequence = len(plan) + 1
        plan.append(
            dict(
                sequence=sequence,
                stage_id=f"stage-{sequence:02d}",
                stage_kind=kind,
                group_id=group_id,
                option_id=option_id,
                depends_on=depends_on,
                quote=quote,
                option=option,
            )
        )

    for option in expected_options(estimate):
        add(
            "baseline",
            option["group_id"],
            option["option_id"],
            option["quote"],
            option["option"],
        )
    enabled = [o for o in target["options"] if o["enabled"]]
    require(plan and enabled)
    add("recorded_piece", target_id, None, target)
    predecessor = plan[-1]["stage_id"]
    for option in enabled:
        add("residual", target_id, option["id"], target, option, predecessor)
    require(len(plan) <= MAX_OPTIONS)
    return plan


def planned_evaluations(estimate: dict) -> list[dict]:
    return stage_plan(estimate) if selected_protocol(estimate) == 2 else expected_options(estimate)


def completed_option(message: dict) -> bool:
    if message.get("stage_kind") == "recorded_piece":
        return False
    if message.get("stage_kind") == "residual" and message["requested"] == 0:
        return True
    return message["result"]["complete"] is True


def _nano(value: str) -> int:
    negative = value.startswith("-")
    whole, _, fraction = value.lstrip("-").partition(".")
    result = int(whole) * 1000000000 + int(fraction.ljust(9, "0") or "0")
    return -result if negative else result


def _inch(value: int) -> str:
    sign = "-" if value < 0 else ""
    whole, fraction = divmod(abs(value), 1000000000)
    return sign + str(whole) + (("." + str(fraction).zfill(9).rstrip("0")) if fraction else "")


def _mm(value: int) -> float:
    return (float(value) / 1000000000) * 25.4


def source_stock(estimate: dict, quote: dict) -> dict:
    """Exact source conversion, not a second geometry/offset implementation."""
    plan = estimate["remnantPlan"]
    evidence = plan["snapshot"]["evidence"]
    shape = evidence["geometry"]
    ox = oy = 0
    if shape["kind"] == "circle":
        ox, oy = _nano(shape["cx"]) - _nano(shape["r"]), _nano(shape["cy"]) - _nano(shape["r"])
    elif shape["kind"] == "polygon":
        ox, oy = (min(_nano(p[a]) for p in shape["outer"]) for a in ("x", "y"))
    else:
        require(shape["kind"] == "rectangle")

    def point(p):
        return {"x": _mm(_nano(p["x"]) - ox), "y": _mm(_nano(p["y"]) - oy)}

    def loop(value):
        if value["kind"] == "circle":
            return {
                "type": "circle",
                "cx": _mm(_nano(value["cx"]) - ox),
                "cy": _mm(_nano(value["cy"]) - oy),
                "r": _mm(_nano(value["r"])),
            }
        return {"type": "poly", "points": [point(p) for p in value["pts"]]}

    holes = []
    if shape["kind"] == "rectangle":
        width, height = _mm(_nano(shape["width"])), _mm(_nano(shape["height"]))
        outer = {
            "type": "poly",
            "points": [
                {"x": 0, "y": 0},
                {"x": width, "y": 0},
                {"x": width, "y": height},
                {"x": 0, "y": height},
            ],
        }
    elif shape["kind"] == "circle":
        outer = loop(shape)
        # Same bounds() arithmetic as the shared source constructor.
        width = (outer["cx"] + outer["r"]) - (outer["cx"] - outer["r"])
        height = (outer["cy"] + outer["r"]) - (outer["cy"] - outer["r"])
    else:
        outer = {"type": "poly", "points": [point(p) for p in shape["outer"]]}
        holes = [{"type": "poly", "points": [point(p) for p in ring]} for ring in shape["holes"]]
        width, height = (max(p[a] for p in outer["points"]) - min(p[a] for p in outer["points"]) for a in ("x", "y"))
    stock = {
        "geometryProfile": quote["geometryProfile"],
        "width": width,
        "height": height,
        "bedWidth": width,
        "bedHeight": height,
        "margin": quote["margin"] * 25.4,
        "gap": quote["gap"] * 25.4,
        "maxSheets": 1,
        "exclusions": [
            {
                **{k: z[k] for k in ("id", "label", "reason")},
                "outline": loop(z["outline"]),
                "clearance": _mm(_nano(plan["zoneClearanceIn"])),
            }
            for z in evidence["unavailable_zones"]
        ],
        "domain": {
            "version": 1,
            "profile": remnant_profile_identity(),
            "outer": outer,
            "holes": holes,
            "sourceOriginIn": {"x": _inch(ox), "y": _inch(oy)},
        },
    }
    if evidence["grain_axis"] is not None:
        stock["grainAxis"] = evidence["grain_axis"]
    return stock


def loop_area(loop: dict) -> float:
    if loop["type"] == "circle":
        return math.pi * loop["r"] * loop["r"]
    points = loop["points"]
    x, y = points[0]["x"], points[0]["y"]
    return (
        abs(
            sum(
                (p["x"] - x) * (q["y"] - y) - (q["x"] - x) * (p["y"] - y)
                for p, q in zip(points, points[1:] + points[:1])
            )
        )
        / 2
    )


def _part_areas(quote: dict) -> dict:
    def converted(loop):
        if loop["type"] == "circle":
            return {**loop, "r": loop["r"] * 25.4}
        return {
            "type": "poly",
            "points": [{a: p[a] * 25.4 for a in ("x", "y")} for p in loop["points"]],
        }

    return {
        part["id"]: (
            loop_area(converted(part["loops"][0])),
            sum(loop_area(converted(h)) for h in part["loops"][1:]),
        )
        for part in quote["parts"]
    }


def validate_domain_leftovers(value: dict, nest: dict, stock: dict, quote: dict) -> None:
    exact(
        value,
        {"inputSignature", "version", "status", "creditUSD", "assumptions", "sheets"},
    )
    require(value["version"] == "werco-leftovers-v4" and value["status"] == "potential_review_only")
    require(type(value["creditUSD"]) is int and value["creditUSD"] == 0)
    require(isinstance(value["inputSignature"], str) and 0 < len(value["inputSignature"]) <= MAX_MESSAGE_BYTES)
    same_source(
        value["assumptions"],
        {
            "profile": {
                "compensatedProfile": geometry_profile_identity(),
                "remnantDomainProfile": remnant_profile_identity(),
                "remnantDomain": remnant_profile_payload(),
            },
            "reservation": DOMAIN_RESERVATION,
            "areaDefinitions": DOMAIN_AREA_DEFINITIONS,
            "internalHolesReserved": True,
            "boundsAreUsableRectangles": False,
            "eligibilityVerified": False,
        },
    )
    require(isinstance(value["sheets"], list) and len(value["sheets"]) == nest["sheets"])
    gross = loop_area(stock["domain"]["outer"]) - sum(loop_area(h) for h in stock["domain"]["holes"])
    part_areas = _part_areas(quote)
    for index, sheet in enumerate(value["sheets"]):
        fields = {
            "grossArea",
            "protectedArea",
            "usableArea",
            "edgeMarginArea",
            "excludedArea",
            "nominalPartArea",
            "reservedCutoutArea",
            "clearanceAndProtectionArea",
            "remainingArea",
        }
        exact(sheet, fields | {"sheet", "regions", "reconciliationResidualArea"})
        tolerance = max(1e-7, gross * 1e-12)
        require(number(sheet["reconciliationResidualArea"], -tolerance, tolerance))
        require(integer(sheet["sheet"], 0, 0) and sheet["sheet"] == index)
        require(all(number(sheet[f], 0, gross + 1e-6) for f in fields))
        _same_number(sheet["grossArea"], gross)
        require(sheet["usableArea"] <= sheet["protectedArea"] <= sheet["grossArea"])
        _same_number(sheet["edgeMarginArea"], gross - sheet["protectedArea"])
        _same_number(sheet["excludedArea"], sheet["protectedArea"] - sheet["usableArea"])
        if not stock["exclusions"]:
            require(sheet["excludedArea"] == 0)
        nominal = sum(
            part_areas[p["partId"]][0] - part_areas[p["partId"]][1] for p in nest["placements"] if p["sheet"] == index
        )
        cutouts = sum(part_areas[p["partId"]][1] for p in nest["placements"] if p["sheet"] == index)
        _same_number(sheet["nominalPartArea"], nominal)
        _same_number(sheet["reservedCutoutArea"], cutouts)
        _same_number(
            sheet["usableArea"],
            sum(
                sheet[f]
                for f in (
                    "remainingArea",
                    "nominalPartArea",
                    "reservedCutoutArea",
                    "clearanceAndProtectionArea",
                )
            )
            + sheet["reconciliationResidualArea"],
        )
        require(isinstance(sheet["regions"], list) and len(sheet["regions"]) <= 2000)
        vertices = 0
        for n, region in enumerate(sheet["regions"]):
            exact(
                region,
                {
                    "id",
                    "outer",
                    "holes",
                    "area",
                    "bounds",
                    "classification",
                    "explanation",
                    "creditUSD",
                },
            )
            require(region["id"] == f"leftover-v4-sheet-{index + 1}-region-{n + 1}")
            require(
                region["classification"] == "review" and type(region["creditUSD"]) is int and region["creditUSD"] == 0
            )
            require(isinstance(region["explanation"], str) and len(region["explanation"]) <= 2000)
            require(number(region["area"]) and isinstance(region["holes"], list))
            for ring in [region["outer"], *region["holes"]]:
                require(isinstance(ring, list) and len(ring) >= 3)
                vertices += len(ring)
                require(vertices <= 30000)
                for point in ring:
                    exact(point, {"x", "y"})
                    require(number(point["x"], 0, stock["width"]) and number(point["y"], 0, stock["height"]))
            area = loop_area({"type": "poly", "points": region["outer"]}) - sum(
                loop_area({"type": "poly", "points": ring}) for ring in region["holes"]
            )
            _same_number(region["area"], area)
            box = exact(region["bounds"], {"x", "y", "width", "height"})
            for axis, dimension in (("x", "width"), ("y", "height")):
                low = min(p[axis] for p in region["outer"])
                _same_number(box[axis], low)
                _same_number(box[dimension], max(p[axis] for p in region["outer"]) - low)
        _same_number(sheet["remainingArea"], sum(r["area"] for r in sheet["regions"]))


def validate_recorded(message: dict, estimate: dict, quote: dict) -> None:
    result = exact(
        message["result"],
        {"nest", "error", "complete", "area"},
        {"leftovers", "leftoverError"},
    )
    require(type(result["complete"]) is bool and number(result["area"]))
    expected_stock = source_stock(estimate, quote)
    if message["stock"] is not None:
        same_source(message["stock"], expected_stock)
    if result["error"] is not None:
        require(isinstance(result["error"], str) and 0 < len(result["error"]) <= 1000)
        require(result["nest"] is None and result["complete"] is False and result["area"] == 0)
        require("leftovers" not in result and "leftoverError" not in result)
        return
    require(message["stock"] is not None)
    nest = validate_nest_structure(result, quote, expected_stock)
    require(nest["sheets"] <= 1)
    gross = loop_area(expected_stock["domain"]["outer"]) - sum(loop_area(h) for h in expected_stock["domain"]["holes"])
    require(number(gross, 1e-12))
    _same_number(result["area"], gross * nest["sheets"])
    areas = _part_areas(quote)
    nominal = sum(areas[p["partId"]][0] - areas[p["partId"]][1] for p in nest["placements"])
    _same_number(nest["area"], nominal)
    _same_number(nest["utilization"], 100 * nominal / gross if nest["sheets"] else 0)
    require(not ("leftovers" in result and "leftoverError" in result))
    if "leftoverError" in result:
        require(isinstance(result["leftoverError"], str) and 0 < len(result["leftoverError"]) <= 1000)
    if "leftovers" in result:
        validate_domain_leftovers(result["leftovers"], nest, expected_stock, quote)


def derive_residual(quote: dict, predecessor: dict) -> tuple[dict, list[dict], int]:
    placed = {p["id"]: set() for p in quote["parts"]}
    for placement in (predecessor["result"]["nest"] or {}).get("placements", []):
        placed[placement["partId"]].add(placement["instance"])
    mapping = [
        {
            "part_id": p["id"],
            "originals": [n for n in range(p["quantity"]) if n not in placed[p["id"]]],
        }
        for p in quote["parts"]
    ]
    mapping = [entry for entry in mapping if entry["originals"]]
    counts = {entry["part_id"]: len(entry["originals"]) for entry in mapping}
    residual = {
        **deepcopy(quote),
        "parts": [{**deepcopy(p), "quantity": counts[p["id"]]} for p in quote["parts"] if p["id"] in counts],
    }
    return residual, mapping, sum(counts.values())


def validate_stage(message: dict, digest: str, estimate: dict, sequence: int, previous: list[dict]) -> tuple[str, int]:
    exact(
        message,
        {
            "type",
            "protocol",
            "input_sha256",
            "sequence",
            "stage_id",
            "stage_kind",
            "group_id",
            "option_id",
            "depends_on",
            "units",
            "requested",
            "instance_map",
            "stock",
            "result",
        },
    )
    require(message["type"] == "stage" and type(message["protocol"]) is int and message["protocol"] == 2)
    require(message["input_sha256"] == digest and message["units"] == "mm")
    plan = stage_plan(estimate)
    require(integer(sequence, 1, len(plan)) and integer(message["sequence"], 1, len(plan)))
    expected = plan[sequence - 1]
    same_source(
        {
            key: message[key]
            for key in (
                "sequence",
                "stage_id",
                "stage_kind",
                "group_id",
                "option_id",
                "depends_on",
            )
        },
        {
            key: expected[key]
            for key in (
                "sequence",
                "stage_id",
                "stage_kind",
                "group_id",
                "option_id",
                "depends_on",
            )
        },
    )
    quote = expected["quote"]
    requested = sum(p["quantity"] for p in quote["parts"])
    kind = expected["stage_kind"]
    if kind == "residual":
        matches = [p for p in previous if p.get("stage_id") == expected["depends_on"]]
        require(len(matches) == 1)
        predecessor = matches[0]
        # Stored prefix must retain its input identity and predecessor semantics;
        # callers verify its canonical digest before providing it here.
        require(
            predecessor["stage_kind"] == "recorded_piece"
            and predecessor["input_sha256"] == digest
            and predecessor["group_id"] == expected["group_id"]
            and predecessor["option_id"] is None
            and predecessor["depends_on"] is None
            and predecessor["instance_map"] is None
        )
        validate_recorded(predecessor, estimate, quote)
        quote, mapping, requested = derive_residual(quote, predecessor)
        require(isinstance(message["instance_map"], list))
        for entry in message["instance_map"]:
            exact(entry, {"part_id", "originals"})
            require(isinstance(entry["originals"], list) and all(integer(n) for n in entry["originals"]))
        same_source(message["instance_map"], mapping)
    else:
        require(message["instance_map"] is None)
    require(integer(message["requested"]) and message["requested"] == requested)
    if kind == "recorded_piece":
        validate_recorded(message, estimate, quote)
    elif not requested:
        require(kind == "residual" and message["stock"] is None and message["result"] is None)
    else:
        # An internal adapter only: the stored frame remains the exact protocol2
        # message, with its own canonical digest, sequence and source identity.
        ordinary = {
            k: message[k]
            for k in (
                "input_sha256",
                "sequence",
                "group_id",
                "option_id",
                "units",
                "requested",
                "stock",
                "result",
            )
        }
        ordinary.update(type="option", protocol=1)
        validate_option(ordinary, digest, {**expected, "quote": quote}, sequence)
    encoded = canonical_json(message).encode("utf-8")
    require(len(encoded) <= MAX_MESSAGE_BYTES)
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


def validate_stage_summary(message: dict, digest: str, keys: list[dict], total: int, complete: int) -> None:
    require(type(message.get("protocol")) is int and message["protocol"] == 2)
    # Same bounded counts, with stage keys already supplied by accepted frames.
    validate_summary({**message, "protocol": 1}, digest, keys, total, complete)
