"""Strict framing and identity checks around the fixed, shared TypeScript kernel.

Python does not replace the geometry validator: the installed, hash-verified Node
bundle validates original contours before emitting any completed option.
"""

import hashlib
import json
import math
import re
from typing import Any

from app.core.nesting_geometry_profile import geometry_profile_payload, is_current_geometry_profile
from app.core.remnant_domain_profile import is_current_remnant_profile
from app.schemas.quote_nesting_runs import MAX_MESSAGE_BYTES, MAX_OPTIONS, SOLVER_VERSION
from app.services.quote_nesting_drafts import canonical_json

EXCLUSION_PROFILE = {
    "version": "werco-stock-exclusions-v1",
    "integerGridMm": 0.0001,
    "circleRadialExcessMm": 0.00254,
    "numericalProtectionMm": 0.0004,
    "partGapFraction": 0.5,
    "offsetJoin": "square-tangent",
    "maximumConvexJoinRadiusFactor": math.sqrt(2),
    "maxRegions": 16,
    "maxSourceVertices": 2000,
    "maxProjectSourceVertices": 20000,
    "maxClearanceMm": 2540,
    "maxGuardedVertices": 60000,
    "maxIntersectionEdgePairs": 8000000,
}
COMPENSATED_RESERVATION = (
    'Compensated outer envelopes reserve half of part gap plus imported curve tolerance and numerical protection; '
    'their interiors are disjoint from other parts and stock exclusions and remain entirely inside the '
    'inward-protected usable sheet. Square tangent joins; internal cutouts reserved. '
    'Not physical kerf or inventory eligibility.'
)


class RunProtocolError(ValueError):
    """A child result cannot be accepted as a validated checkpoint."""


def require(ok: Any) -> None:
    if not ok:
        raise RunProtocolError("Invalid nesting worker protocol")


def exact(value: Any, required: set[str], optional: set[str] | None = None) -> dict:
    require(isinstance(value, dict))
    require(required.issubset(value) and set(value).issubset(required | (optional or set())))
    return value


def integer(value: Any, low: int = 0, high: int = 300) -> bool:
    return type(value) is int and low <= value <= high


def number(value: Any, low: float = 0, high: float = math.inf) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _unique(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def parse_message(raw: bytes) -> dict:
    require(0 < len(raw) <= MAX_MESSAGE_BYTES)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
        stack = [(value, 0)]
        nodes = 0
        while stack:
            item, depth = stack.pop()
            nodes += 1
            require(depth <= 32 and nodes <= 1000000)
            if isinstance(item, (dict, list)):
                require(nodes + len(stack) + len(item) <= 1000000)
                stack.extend((child, depth + 1) for child in (item.values() if isinstance(item, dict) else item))
            elif type(item) in (float, int):
                require(math.isfinite(item))
        require(isinstance(value, dict))
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as exc:
        raise RunProtocolError("Invalid nesting worker protocol") from exc


def expected_options(estimate: dict) -> list[dict]:
    result = []
    for group in estimate["groups"]:
        quote = group["quote"]
        requested = sum(part["quantity"] for part in quote["parts"])
        if not requested:
            continue
        for option in quote["options"]:
            if option["enabled"]:
                result.append({"group_id": group["id"], "option_id": option["id"], "quote": quote, "option": option})
    return result


def _base(message: dict, kind: str, digest: str) -> None:
    require(message.get("type") == kind and message.get("protocol") == 1 and type(message.get("protocol")) is int)
    require(message.get("input_sha256") == digest)


def validate_hello(message: dict, digest: str, manifest: dict, *, protocol: int = 1) -> None:
    exact(
        message,
        {
            "type",
            "protocol",
            "input_sha256",
            "solver_version",
            "bundle_sha256",
            "node_version",
            "units",
            "geometry_profile",
        }
        | ({"remnant_domain_profile"} if protocol == 2 else set()),
    )
    require(type(message.get("protocol")) is int and message["protocol"] == protocol)
    require(message.get("type") == "hello" and message.get("input_sha256") == digest)
    if protocol == 2:
        require(is_current_remnant_profile(message["remnant_domain_profile"]))
        require(message["remnant_domain_profile"] == manifest.get("remnant_domain_profile"))
    require(message["solver_version"] == SOLVER_VERSION == manifest["solver_version"])
    require(is_current_geometry_profile(message['geometry_profile']))
    require(message['geometry_profile'] == manifest.get('geometry_profile'))
    require(message["bundle_sha256"] == manifest["bundle_sha256"] and message["units"] == "mm")
    require(isinstance(message["node_version"], str) and re.fullmatch(r"v22\.\d+\.\d+", message["node_version"]))


def _same_number(actual: Any, expected: float) -> None:
    require(number(actual) and math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-7))


def _same_inches(actual: Any, inches: float) -> None:
    # Both interpreters use IEEE binary64 for the identical one multiplication.
    # Source dimensions/allowances are identities, not measured comparisons.
    require(number(actual) and actual == inches * 25.4)


def _same_exclusions(actual: dict, source: dict) -> None:
    """Bind the emitted source geometry, not its separately guarded envelope."""
    require(("exclusions" in actual) == ("exclusions" in source))
    if "exclusions" not in source:
        return
    regions = actual["exclusions"]
    require(isinstance(regions, list) and len(regions) == len(source["exclusions"]))

    for region, original in zip(regions, source["exclusions"]):
        exact(region, {"id", "label", "reason", "outline", "clearance"})
        require(all(region[field] == original[field] for field in ("id", "label", "reason")))
        _same_inches(region["clearance"], original["clearance"])
        outline, saved = region["outline"], original["outline"]
        if saved["type"] == "circle":
            exact(outline, {"type", "cx", "cy", "r"})
            require(outline["type"] == "circle")
            for field in ("cx", "cy", "r"):
                _same_inches(outline[field], saved[field])
        else:
            exact(outline, {"type", "points"})
            require(outline["type"] == "poly" and isinstance(outline["points"], list))
            require(len(outline["points"]) == len(saved["points"]))
            for point, saved_point in zip(outline["points"], saved["points"]):
                exact(point, {"x", "y"})
                for axis in ("x", "y"):
                    _same_inches(point[axis], saved_point[axis])


def validate_option(message: dict, digest: str, expected: dict, sequence: int) -> tuple[str, int]:
    exact(
        message,
        {
            "type",
            "protocol",
            "input_sha256",
            "sequence",
            "group_id",
            "option_id",
            "units",
            "requested",
            "stock",
            "result",
        },
    )
    _base(message, "option", digest)
    require(integer(message["sequence"], 1, MAX_OPTIONS) and message["sequence"] == sequence)
    require(message["group_id"] == expected["group_id"] and message["option_id"] == expected["option_id"])
    require(message["units"] == "mm")
    quote, option = expected["quote"], expected["option"]
    require(quote['version'] == 14 and is_current_geometry_profile(quote.get('geometryProfile')))
    requested = sum(part["quantity"] for part in quote["parts"])
    require(integer(message["requested"], 1) and message["requested"] == requested)
    stock = exact(
        message["stock"],
        {"width", "height", "margin", "gap", "maxSheets", "bedWidth", "bedHeight", "geometryProfile"},
        {"grainAxis", "exclusions"},
    )
    require(stock['geometryProfile'] == quote['geometryProfile'])
    for field in ("width", "height"):
        _same_inches(stock[field], option[field])
        _same_inches(stock["bed" + field.title()], option[field])
    for field in ("margin", "gap"):
        _same_inches(stock[field], quote[field])
    require(integer(stock["maxSheets"], 1) and stock["maxSheets"] == requested)
    require(stock.get("grainAxis") == quote.get("grainAxis"))
    require(('grainAxis' in stock) == ('grainAxis' in quote))
    _same_exclusions(stock, option)
    result = exact(
        message["result"], {"option", "nest", "error", "complete", "area", "cost"}, {"leftovers", "leftoverError"}
    )
    result_option = exact(result["option"], {"id", "width", "height", "enabled", "price"}, {"exclusions"})
    require(result_option["id"] == option["id"] and result_option["enabled"] is True)
    for field in ("width", "height"):
        _same_inches(result_option[field], option[field])
    require(result_option["price"] == option["price"] and type(result["complete"]) is bool)
    _same_exclusions(result_option, option)
    require(number(result["area"]) and (result["cost"] is None or number(result["cost"])))
    if result["error"] is not None:
        require(isinstance(result["error"], str) and len(result["error"]) <= 1000)
        require(
            result["nest"] is None and result["complete"] is False and result["area"] == 0 and result["cost"] is None
        )
        require("leftovers" not in result and "leftoverError" not in result)
    else:
        nest = validate_nest_structure(result, quote, stock)
        _same_number(result["area"], stock["width"] * stock["height"] * nest["sheets"])
        # Saved catalog acknowledgments may make cost unavailable; never infer a price.
        if result["cost"] is not None:
            require(option["price"] is not None)
            _same_number(result["cost"], option["price"] * nest["sheets"])
        require(not ("leftovers" in result and "leftoverError" in result))
        if "leftoverError" in result:
            require(isinstance(result["leftoverError"], str) and len(result["leftoverError"]) <= 1000)
        if "leftovers" in result:
            _validate_leftover_status(result["leftovers"], nest["sheets"], stock)
    canonical = canonical_json(message)
    byte_count = len(canonical.encode("utf-8"))
    require(byte_count <= MAX_MESSAGE_BYTES)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), byte_count


def _validate_leftover_status(value: Any, sheets: int, stock: dict) -> None:
    exact(value, {"inputSignature", "version", "status", "creditUSD", "assumptions", "sheets"})
    require(value["version"] == 'werco-leftovers-v3')
    require(value["status"] == "potential_review_only" and type(value["creditUSD"]) is int and value["creditUSD"] == 0)
    assumptions = exact(
        value["assumptions"],
        {"profile", "reservation", "internalHolesReserved", "boundsAreUsableRectangles", "eligibilityVerified"},
    )
    require(
        assumptions["internalHolesReserved"] is True
        and assumptions["boundsAreUsableRectangles"] is False
        and assumptions["eligibilityVerified"] is False
    )
    require(assumptions['reservation'] == COMPENSATED_RESERVATION)
    require(canonical_json(assumptions['profile']) == canonical_json(geometry_profile_payload()))
    require(isinstance(value["sheets"], list) and len(value["sheets"]) == sheets)
    for index, sheet in enumerate(value["sheets"]):
        require(isinstance(sheet, dict) and isinstance(sheet.get("regions"), list))
        require(integer(sheet.get("sheet")) and sheet["sheet"] == index)
        area_fields = {
            "grossArea",
            "usableArea",
            "edgeMarginArea",
            "nominalPartArea",
            "reservedCutoutArea",
            "clearanceAndProtectionArea",
            "remainingArea",
        }
        require(all(number(sheet.get(field)) for field in area_fields))
        require(number(sheet.get('excludedArea'), 0, sheet['usableArea']))
        if not stock.get('exclusions'):
            require(sheet['excludedArea'] == 0)
        residual = sheet.get("reconciliationResidualArea")
        require(number(residual, -math.inf))
        require(abs(residual) <= max(1e-7, sheet["grossArea"] * 1e-12))
        _same_number(sheet["grossArea"], stock["width"] * stock["height"])
        _same_number(
            sheet["usableArea"], (stock["width"] - 2 * stock["margin"]) * (stock["height"] - 2 * stock["margin"])
        )
        _same_number(sheet["edgeMarginArea"] + sheet["usableArea"], sheet["grossArea"])
        _same_number(
            sheet["grossArea"],
            sum(sheet[field] for field in area_fields - {"grossArea", "usableArea"})
            + sheet.get("excludedArea", 0)
            + residual,
        )
        for region in sheet["regions"]:
            require(isinstance(region, dict) and region.get("classification") == "review")
            require(type(region.get("creditUSD")) is int and region["creditUSD"] == 0)


def validate_summary(message: dict, digest: str, keys: list[dict], total: int, complete: int) -> None:
    exact(
        message,
        {
            "type",
            "protocol",
            "input_sha256",
            "evaluated_keys",
            "evaluated_count",
            "complete_option_count",
            "total_options",
            "stop_reason",
        },
    )
    _base(message, "summary", digest)
    require(message["evaluated_keys"] == keys)
    require(integer(message["evaluated_count"], 0, MAX_OPTIONS) and message["evaluated_count"] == len(keys))
    require(integer(message["complete_option_count"], 0, MAX_OPTIONS) and message["complete_option_count"] == complete)
    require(integer(message["total_options"], 0, 3600) and message["total_options"] == total)
    require(message["stop_reason"] == ("work_limit" if total > MAX_OPTIONS else "completed"))
    require(len(keys) == min(total, MAX_OPTIONS))


def validate_nest_structure(result: dict, quote: dict, stock: dict) -> dict:
    """Bounded identity/count checks shared by ordinary and recorded-piece stages."""
    requested = sum(part["quantity"] for part in quote["parts"])
    nest = exact(
        result["nest"],
        {"placements", "unplaced", "sheets", "area", "utilization", "method"},
    )
    require(integer(nest["sheets"]) and number(nest["area"]) and number(nest["utilization"], 0, 100.00000001))
    require(isinstance(nest["method"], str) and len(nest["method"]) <= 300)
    require(isinstance(nest["placements"], list) and len(nest["placements"]) <= requested)
    require(isinstance(nest["unplaced"], list) and len(nest["unplaced"]) <= len(quote["parts"]))
    quantities = {part["id"]: part["quantity"] for part in quote["parts"]}
    placed = {key: set() for key in quantities}
    used_sheets = set()
    for placement in nest["placements"]:
        exact(
            placement,
            {"partId", "instance", "x", "y", "width", "height", "rotation", "sheet"},
        )
        part_id = placement["partId"]
        require(part_id in quantities and integer(placement["instance"], 0, quantities[part_id] - 1))
        require(placement["instance"] not in placed[part_id])
        placed[part_id].add(placement["instance"])
        require(integer(placement["sheet"], 0, nest["sheets"] - 1))
        used_sheets.add(placement["sheet"])
        require(type(placement["rotation"]) is int and placement["rotation"] in (0, 90, 180, 270))
        for axis, dimension in (("x", "width"), ("y", "height")):
            require(number(placement[axis], -1e-7, stock[dimension] + 1e-7))
            require(number(placement[dimension], 0, stock[dimension] + 1e-7))
            require(placement[axis] + placement[dimension] <= stock[dimension] + 1e-6)
    remaining = {}
    for unplaced in nest["unplaced"]:
        exact(unplaced, {"partId", "count", "reason"})
        require(unplaced["partId"] in quantities and unplaced["partId"] not in remaining)
        require(
            integer(unplaced["count"], 1) and isinstance(unplaced["reason"], str) and len(unplaced["reason"]) <= 1000
        )
        remaining[unplaced["partId"]] = unplaced["count"]
    require(all(len(placed[key]) + remaining.get(key, 0) == qty for key, qty in quantities.items()))
    require(used_sheets == set(range(nest["sheets"])))
    require(result["complete"] == (not remaining))
    return nest
