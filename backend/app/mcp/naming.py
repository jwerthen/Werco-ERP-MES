"""Tool-name derivation and the collision policy for the generated MCP catalog.

Pure: no app imports, no I/O. ``catalog.py`` is the only consumer.

WHY names are derived, not hand-maintained: the catalog is built from ``app.openapi()``
at startup (brief rule 3), so a router that ships tomorrow is a tool tomorrow with no
list to update. The price is that a name can SHIFT when a collision appears: a bare
``start_operation`` becomes ``work_orders_start_operation`` the day a second router
defines ``start_operation``. Convenience tools (``convenience.py``) have fixed names for
exactly that reason -- they are the stable handles an agent prompt can rely on.
"""

from __future__ import annotations

import re
from typing import Dict, Hashable, Iterable, Mapping, Tuple, TypeVar

# MCP tool names must match this (SDK-enforced on the wire).
TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
MAX_TOOL_NAME_LENGTH = 64

# FastAPI's default operationId is ``<function>_<path with / -> _>_<method>``; every
# router here is mounted under /api/v1, so the function name is the prefix before this.
_OPERATION_ID_MARKER = "_api_v1_"
_ENDPOINT_SUFFIX = "_endpoint"

_UNSAFE_CHARS = re.compile(r"[^a-zA-Z0-9_-]+")
_UNDERSCORE_RUNS = re.compile(r"_{2,}")

K = TypeVar("K", bound=Hashable)


def function_name_from_operation_id(operation_id: str, method: str) -> str:
    """Return the route function's name as derived from a FastAPI operationId.

    ``create_work_order_api_v1_work_orders__post`` -> ``create_work_order``. Falls back
    to stripping a trailing ``_<method>`` when the ``_api_v1_`` marker is absent (the
    health endpoints, which the catalog excludes anyway), then drops a trailing
    ``_endpoint`` (``create_manual_laser_nest_endpoint`` -> ``create_manual_laser_nest``).
    """
    name = operation_id or ""
    if _OPERATION_ID_MARKER in name:
        name = name.split(_OPERATION_ID_MARKER, 1)[0]
    else:
        suffix = f"_{method.lower()}"
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    if name.endswith(_ENDPOINT_SUFFIX) and len(name) > len(_ENDPOINT_SUFFIX):
        name = name[: -len(_ENDPOINT_SUFFIX)]
    return sanitize_name_fragment(name) or "operation"


def sanitize_name_fragment(fragment: str) -> str:
    """Reduce ``fragment`` to the tool-name alphabet: non-alphanumerics -> ``_``, collapsed."""
    cleaned = _UNSAFE_CHARS.sub("_", fragment)
    cleaned = _UNDERSCORE_RUNS.sub("_", cleaned)
    return cleaned.strip("_")


def tag_slug(tag: str) -> str:
    """``"Shop Floor"`` -> ``shop_floor``; ``"Customer Complaints & RMA"`` -> ``customer_complaints_rma``."""
    slug = re.sub(r"[^a-z0-9]+", "_", tag.lower()).strip("_")
    return slug or "untagged"


def is_valid_tool_name(name: str) -> bool:
    return bool(TOOL_NAME_PATTERN.match(name))


def fit_tool_name(name: str) -> str:
    """Truncate to the 64-char cap without leaving a dangling separator."""
    return name[:MAX_TOOL_NAME_LENGTH].rstrip("_-") or "operation"


def prefixed_tool_name(slug: str, function_name: str) -> str:
    """``<tag_slug>_<function_name>`` fitted to 64 chars: the slug gives way first, then the name."""
    room_for_slug = MAX_TOOL_NAME_LENGTH - len(function_name) - 1
    if room_for_slug >= 1:
        trimmed_slug = slug[:room_for_slug].rstrip("_-")
        if trimmed_slug:
            return f"{trimmed_slug}_{function_name}"
    return fit_tool_name(function_name)


def assign_tool_names(entries: Mapping[K, Tuple[str, str]], *, reserved: Iterable[str] = ()) -> Dict[K, str]:
    """Map every catalog entry to a unique tool name.

    ``entries`` maps an opaque key (the executor uses ``(METHOD, path)``) to
    ``(function_name, tag)``. A function name that is unique across the catalog is
    used bare; when two or more entries share one, EVERY member of the collision
    gets ``<tag_slug>_<function_name>`` -- none of them keeps the bare name, so the
    bare name never silently points at whichever router happened to sort first.

    ``reserved`` names (the fixed convenience-tool names) count as an extra, standing
    claim on the bare form: a lone ``search`` route would still surface as
    ``<tag>_search`` rather than fight the convenience tool for the name.

    Raises ``ValueError`` if uniqueness cannot be achieved (two colliding entries
    under the same tag, or a truncation that folds two names together): a catalog
    with an ambiguous name is worse than no catalog.
    """
    reserved_names = set(reserved)
    members_by_function: Dict[str, int] = {}
    for function_name, _tag in entries.values():
        members_by_function[function_name] = members_by_function.get(function_name, 0) + 1

    names: Dict[K, str] = {}
    for key, (function_name, tag) in entries.items():
        if members_by_function[function_name] == 1 and function_name not in reserved_names:
            names[key] = fit_tool_name(function_name)
        else:
            names[key] = prefixed_tool_name(tag_slug(tag), function_name)

    owners: Dict[str, K] = {}
    for key, name in names.items():
        if not is_valid_tool_name(name):
            raise ValueError(f"Derived tool name {name!r} for {key!r} is not a valid MCP tool name")
        if name in owners:
            raise ValueError(f"Tool name {name!r} is claimed by both {owners[name]!r} and {key!r}")
        owners[name] = key
    return names
