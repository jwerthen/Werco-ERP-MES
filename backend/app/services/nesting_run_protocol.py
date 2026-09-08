"""Strict framing and identity checks around the fixed, shared TypeScript kernel.

Python does not replace the geometry validator: the installed, hash-verified Node
bundle validates original contours before emitting any completed option.
"""

import hashlib
import json
import math
import re
from typing import Any

from app.schemas.quote_nesting_runs import MAX_MESSAGE_BYTES, MAX_OPTIONS, SOLVER_VERSION
from app.services.quote_nesting_drafts import canonical_json


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


def validate_hello(message: dict, digest: str, manifest: dict) -> None:
    exact(message, {"type", "protocol", "input_sha256", "solver_version", "bundle_sha256", "node_version", "units"})
    _base(message, "hello", digest)
    require(message["solver_version"] == SOLVER_VERSION == manifest["solver_version"])
    require(message["bundle_sha256"] == manifest["bundle_sha256"] and message["units"] == "mm")
    require(isinstance(message["node_version"], str) and re.fullmatch(r"v22\.\d+\.\d+", message["node_version"]))


def _same_number(actual: Any, expected: float) -> None:
    require(number(actual) and math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-7))


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
    requested = sum(part["quantity"] for part in quote["parts"])
    require(integer(message["requested"], 1) and message["requested"] == requested)
    stock = exact(
        message["stock"], {"width", "height", "margin", "gap", "maxSheets", "bedWidth", "bedHeight"}, {"grainAxis"}
    )
    for field in ("width", "height"):
        _same_number(stock[field], option[field] * 25.4)
        _same_number(stock["bed" + field.title()], option[field] * 25.4)
    for field in ("margin", "gap"):
        _same_number(stock[field], quote[field] * 25.4)
    require(integer(stock["maxSheets"], 1) and stock["maxSheets"] == requested)
    require(stock.get("grainAxis") == quote.get("grainAxis"))
    result = exact(
        message["result"], {"option", "nest", "error", "complete", "area", "cost"}, {"leftovers", "leftoverError"}
    )
    result_option = exact(result["option"], {"id", "width", "height", "enabled", "price"})
    require(result_option["id"] == option["id"] and result_option["enabled"] is True)
    for field in ("width", "height"):
        _same_number(result_option[field], option[field] * 25.4)
    require(result_option["price"] == option["price"] and type(result["complete"]) is bool)
    require(number(result["area"]) and (result["cost"] is None or number(result["cost"])))
    if result["error"] is not None:
        require(isinstance(result["error"], str) and len(result["error"]) <= 1000)
        require(
            result["nest"] is None and result["complete"] is False and result["area"] == 0 and result["cost"] is None
        )
        require("leftovers" not in result and "leftoverError" not in result)
    else:
        nest = exact(result["nest"], {"placements", "unplaced", "sheets", "area", "utilization", "method"})
        require(integer(nest["sheets"]) and number(nest["area"]) and number(nest["utilization"], 0, 100.00000001))
        require(isinstance(nest["method"], str) and len(nest["method"]) <= 300)
        require(isinstance(nest["placements"], list) and len(nest["placements"]) <= requested)
        require(isinstance(nest["unplaced"], list) and len(nest["unplaced"]) <= len(quote["parts"]))
        quantities = {part["id"]: part["quantity"] for part in quote["parts"]}
        placed = {key: set() for key in quantities}
        used_sheets = set()
        for placement in nest["placements"]:
            exact(placement, {"partId", "instance", "x", "y", "width", "height", "rotation", "sheet"})
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
                integer(unplaced["count"], 1)
                and isinstance(unplaced["reason"], str)
                and len(unplaced["reason"]) <= 1000
            )
            remaining[unplaced["partId"]] = unplaced["count"]
        require(all(len(placed[key]) + remaining.get(key, 0) == qty for key, qty in quantities.items()))
        require(used_sheets == set(range(nest["sheets"])))
        require(result["complete"] == (not remaining))
        _same_number(result["area"], stock["width"] * stock["height"] * nest["sheets"])
        # Saved catalog acknowledgments may make cost unavailable; never infer a price.
        if result["cost"] is not None:
            require(option["price"] is not None)
            _same_number(result["cost"], option["price"] * nest["sheets"])
        require(not ("leftovers" in result and "leftoverError" in result))
        if "leftoverError" in result:
            require(isinstance(result["leftoverError"], str) and len(result["leftoverError"]) <= 1000)
        if "leftovers" in result:
            _validate_leftover_status(result["leftovers"], nest["sheets"])
    canonical = canonical_json(message)
    byte_count = len(canonical.encode("utf-8"))
    require(byte_count <= MAX_MESSAGE_BYTES)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), byte_count


def _validate_leftover_status(value: Any, sheets: int) -> None:
    exact(value, {"inputSignature", "version", "status", "creditUSD", "assumptions", "sheets"})
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
    require(isinstance(value["sheets"], list) and len(value["sheets"]) == sheets)
    for sheet in value["sheets"]:
        require(isinstance(sheet, dict) and isinstance(sheet.get("regions"), list))
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
