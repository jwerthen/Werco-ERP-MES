"""Tool-name derivation and the collision policy for the generated MCP catalog.

Pure: no app imports, no I/O. ``catalog.py`` is the only consumer.

WHY names are derived, not hand-maintained: the catalog is built from ``app.openapi()``
at startup (brief rule 3), so a router that ships tomorrow is a tool tomorrow with no
list to update. The price is that a name can SHIFT when a collision appears: a bare
``start_operation`` becomes ``work_orders_start_operation`` the day a second router
defines ``start_operation``. Convenience tools (``convenience.py``) have fixed names for
exactly that reason -- they are the stable handles an agent prompt can rely on.

THE NAMING RULE, in the order ``assign_tool_names`` applies it:

1. The function name (``function_name_from_operation_id``) is used BARE when it is
   unique across the catalog and not a reserved convenience name.
2. A COLLISION -- two or more routers defining the same function name -- prefixes
   EVERY member with its tag slug (``shop_floor_start_operation`` /
   ``work_orders_start_operation``). None keeps the bare name.
3. The prefix PROPAGATES TO THE FAMILY. Each function name has a NOUN PHRASE: the
   tokens after the first (verb) token, with the last token singularized --
   ``list_machines`` -> ``machine``, ``delete_work_order`` -> ``work_order``,
   ``list_finishes`` -> ``finish``, ``get_material`` -> ``material``. Within one tag,
   the noun phrases of the collision-prefixed functions form a set, and every OTHER
   function in that tag whose noun phrase is in the set is prefixed with the tag slug
   too. A resource family therefore reads as one family: Admin Settings has
   ``admin_settings_list_machines`` / ``admin_settings_create_machine`` (collisions with
   Quote Calculator) AND ``admin_settings_update_machine`` /
   ``admin_settings_delete_machine`` (propagated), rather than two prefixed and two
   bare members. Likewise ``materials_supplies_get_material``,
   ``routing_delete_operation`` / ``routing_reorder_operations``,
   ``shop_floor_resume_operation``, ``work_orders_delete_work_order`` /
   ``work_orders_restore_work_order``. A reserved-name prefix (rule 4) does NOT seed
   propagation: every generated function that carries a convenience name is shadowed
   by that convenience tool, so there is no visible family split to repair, and
   seeding from it would rename every ``*_part`` / ``*_inventory`` / ``*_work_center``
   tool. Propagation is one pass: a propagated member's noun phrase is already in the
   set by construction, so it can never widen the set further.
4. A RESERVED name (a convenience tool's fixed name) pushes a lone generated function
   onto the prefixed form.
5. Names are fitted to 64 characters (slug trimmed first, then the name) and a residual
   collision gets a deterministic numeric suffix rather than raising.

Singularization (``singularize_token``) is deliberately dumb and only has to be
CONSISTENT between the members of one family: ``ies`` -> ``y``; ``es`` stripped when the
stem ends in ``sh`` / ``ch`` / ``x`` / ``s`` / ``z`` (``finishes`` -> ``finish``,
``losses`` -> ``loss``); otherwise a trailing ``s`` stripped (``machines`` ->
``machine``, ``orders`` -> ``order``), except that a token ending in ``ss`` is left
alone (``process``, ``address`` are singular). Known false negative: ``clauses`` ->
``claus`` while ``clause`` stays ``clause``, so a ``list_clauses`` collision would not
reach ``create_clause``; that costs a missed propagation, never a wrong one.

THE CONSEQUENCE: a generated name can shift not only when ITS OWN twin appears but
when a SIBLING'S does -- ``delete_work_order`` became ``work_orders_delete_work_order``
because the maintenance router defines ``list_work_orders`` / ``start_work_order``,
not because anyone else defines ``delete_work_order``. Anchor prompts on the
convenience tools; ``tests/test_mcp_docs_names.py`` is what keeps the docs honest.
"""

from __future__ import annotations

import re
from typing import Dict, Hashable, Iterable, Mapping, Set, Tuple, TypeVar

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


def singularize_token(token: str) -> str:
    """The deliberately dumb singular of one name token (see the module docstring).

    ``machines`` -> ``machine``, ``finishes`` -> ``finish``, ``entries`` -> ``entry``,
    ``losses`` -> ``loss``, ``process`` -> ``process``, ``material`` -> ``material``.
    """
    if token.endswith("ies") and len(token) > 3:
        return token[:-3] + "y"
    if token.endswith("es") and len(token) > 2 and token[:-2].endswith(("sh", "ch", "x", "s", "z")):
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss") and len(token) > 1:
        return token[:-1]
    return token


def noun_phrase(function_name: str) -> str:
    """The tokens after the first (verb) token, last one singularized; ``""`` for a one-token name.

    ``list_work_orders`` -> ``work_order``, ``delete_machine`` -> ``machine``,
    ``get_all_operations`` -> ``all_operation``, ``search`` -> ``""``.
    """
    tokens = [token for token in function_name.split("_") if token]
    if len(tokens) < 2:
        return ""
    rest = tokens[1:]
    rest[-1] = singularize_token(rest[-1])
    return "_".join(rest)


def family_prefixed_keys(entries: Mapping[K, Tuple[str, str]]) -> Set[K]:
    """The keys the tag prefix reaches: the collisions themselves plus their family (rules 2 and 3).

    A collision is a function name shared by two or more entries. Within each tag,
    the noun phrases of that tag's colliding functions seed a set, and every entry
    in the tag whose noun phrase is in the set is prefixed too. Reserved names are
    not an input here (see the module docstring for why they do not seed).
    """
    members_by_function: Dict[str, int] = {}
    for function_name, _tag in entries.values():
        members_by_function[function_name] = members_by_function.get(function_name, 0) + 1
    collided = {function_name for function_name, count in members_by_function.items() if count > 1}

    family_nouns: Dict[str, Set[str]] = {}
    for function_name, tag in entries.values():
        if function_name in collided:
            noun = noun_phrase(function_name)
            if noun:
                family_nouns.setdefault(tag, set()).add(noun)

    prefixed: Set[K] = set()
    for key, (function_name, tag) in entries.items():
        if function_name in collided or noun_phrase(function_name) in family_nouns.get(tag, set()):
            prefixed.add(key)
    return prefixed


def assign_tool_names(entries: Mapping[K, Tuple[str, str]], *, reserved: Iterable[str] = ()) -> Dict[K, str]:
    """Map every catalog entry to a unique tool name.

    ``entries`` maps an opaque key (the executor uses ``(METHOD, path)``) to
    ``(function_name, tag)``. A function name that is unique across the catalog is
    used bare; when two or more entries share one, EVERY member of the collision
    gets ``<tag_slug>_<function_name>`` -- none of them keeps the bare name, so the
    bare name never silently points at whichever router happened to sort first --
    and the prefix propagates to the tag's siblings that share the collision's noun
    phrase (``family_prefixed_keys``), so a resource family is prefixed as a whole.

    ``reserved`` names (the fixed convenience-tool names) count as an extra, standing
    claim on the bare form: a lone ``search`` route would still surface as
    ``<tag>_search`` rather than fight the convenience tool for the name.

    The policy is TOTAL. A residual collision -- two entries under one tag with the
    same function name, or a 64-char fit that folds two names together -- gets a
    deterministic numeric suffix (``_2``, ``_3``, ...) in sorted-key order rather than
    raising. ``build_door`` runs at import of ``app.main``, so a raise here with the
    HTTP door enabled would keep the whole API from booting over a naming nicety;
    ``tests/test_mcp_catalog.py`` is where a suffixed name is meant to be noticed.
    """
    reserved_names = set(reserved)
    prefixed = family_prefixed_keys(entries)

    names: Dict[K, str] = {}
    for key, (function_name, tag) in entries.items():
        if key in prefixed or function_name in reserved_names:
            names[key] = prefixed_tool_name(tag_slug(tag), function_name)
        else:
            names[key] = fit_tool_name(function_name)

    taken: Set[str] = set(names.values()) | reserved_names
    owners: Dict[str, K] = {}
    for key in sorted(names, key=repr):
        name = names[key]
        if name not in owners:
            owners[name] = key
            continue
        counter = 2
        candidate = _suffixed_tool_name(name, counter)
        while candidate in taken or candidate in owners:
            counter += 1
            candidate = _suffixed_tool_name(name, counter)
        names[key] = candidate
        owners[candidate] = key
        taken.add(candidate)

    for key, name in names.items():
        if not is_valid_tool_name(name):  # unreachable after sanitisation; kept as the wire contract
            raise ValueError(f"Derived tool name {name!r} for {key!r} is not a valid MCP tool name")
    return names


def _suffixed_tool_name(name: str, counter: int) -> str:
    """``name`` + ``_<counter>``, still within the 64-char cap (the name gives way, never the suffix)."""
    suffix = f"_{counter}"
    base = name[: MAX_TOOL_NAME_LENGTH - len(suffix)].rstrip("_-") or "operation"
    return f"{base}{suffix}"
