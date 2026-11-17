"""Values a JSON response cannot render: unencodable text, and structure nested too deeply.

**Unencodable text.** JSON allows a lone surrogate escape (``"\\ud800"``) and ``json.loads``
keeps it as the code point U+D800 -- which a Python ``str`` can carry but no UTF-8 encoder
accepts. So a request body can deliver one into any ``Any``-typed or ``str`` field, it can
travel all the way to a JSON column (``json.dumps`` escapes it back to ``\\ud800``, and
Postgres ``json`` stores that), and then every response that renders it raises
``UnicodeEncodeError`` -- after the route has already run, as a 500 nothing upstream can
catch. A stored one 500s every later read of that row. The surrogate range U+D800-U+DFFF
is the ONLY set of code points a ``str`` can hold that strict UTF-8 refuses, so the checks
below are exact, not a heuristic.

**Non-finite numbers.** Python's ``json.loads`` also accepts the non-standard ``NaN`` /
``Infinity`` / ``-Infinity`` literals, and the JSON response refuses to encode them ("Out of
range float values are not JSON compliant"). ``contains_unrenderable`` / ``make_renderable``
handle both kinds of leaf -- unencodable text and non-finite floats -- for a value that is
about to be echoed back (the 422 handler).

**Depth.** pydantic's serializer refuses a value nested past its recursion limit (about
255), so a stored value nested that deep is a 500 on every read too. ``exceeds_depth`` /
``cut_below_depth`` bound it.

**Every walker here is ITERATIVE** (an explicit stack, never recursion). They run on
request bodies an unauthenticated caller chooses -- the app-wide 422 handler walks every
echoed ``input`` -- and a recursive walk turned a ``[[[...]]]`` body a few hundred levels
deep (under 1 KB) into a ``RecursionError``, i.e. a 500, an ERROR line and a Sentry event
per request. Containers are ``dict`` (keys and values), ``list`` and ``tuple``; anything
else is a leaf and is not looked into.
"""

import math
import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

_UNENCODABLE = re.compile("[\ud800-\udfff]")

# What an unencodable character -- or a value cut off for depth -- is shown as once it has
# to be rendered anyway.
REPLACEMENT_CHARACTER = "�"

_CONTAINERS = (dict, list, tuple)


def is_utf8_encodable(text: str) -> bool:
    """Whether ``text`` can be encoded as UTF-8 (holds no unpaired surrogate)."""
    return _UNENCODABLE.search(text) is None


def replace_unencodable(text: str) -> str:
    """``text`` with each unpaired surrogate replaced by U+FFFD (unchanged when there is none)."""
    return _UNENCODABLE.sub(REPLACEMENT_CHARACTER, text)


def _is_non_finite(value: Any) -> bool:
    return isinstance(value, float) and not math.isfinite(value)


def non_finite_stand_in(value: float) -> str:
    """The JSON-safe stand-in for a non-finite float: ``"NaN"``, ``"Infinity"`` or ``"-Infinity"``
    -- the literal the caller sent, as a string."""
    if math.isnan(value):
        return "NaN"
    return "Infinity" if value > 0 else "-Infinity"


def _renderable_leaf(value: Any) -> Any:
    return non_finite_stand_in(value) if _is_non_finite(value) else value


def _children(value: Any) -> List[Any]:
    """A container's direct members (a dict's keys AND values)."""
    if isinstance(value, dict):
        members: List[Any] = []
        for key, item in value.items():
            members.append(key)
            members.append(item)
        return members
    return list(value)


def contains_unencodable_text(value: Any) -> bool:
    """Whether any string in ``value`` -- dict keys included -- cannot be encoded as UTF-8."""
    stack: List[Any] = [value]
    seen: Set[int] = set()
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            if not is_utf8_encodable(current):
                return True
        elif isinstance(current, _CONTAINERS) and id(current) not in seen:
            seen.add(id(current))
            stack.extend(_children(current))
    return False


def contains_unrenderable(value: Any) -> bool:
    """Whether ``value`` holds anything a JSON response cannot encode: a string with an
    unpaired surrogate (dict keys included) or a non-finite float."""
    stack: List[Any] = [value]
    seen: Set[int] = set()
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            if not is_utf8_encodable(current):
                return True
        elif _is_non_finite(current):
            return True
        elif isinstance(current, _CONTAINERS) and id(current) not in seen:
            seen.add(id(current))
            stack.extend(_children(current))
    return False


def exceeds_depth(value: Any, max_depth: int) -> bool:
    """Whether ``value`` nests containers more than ``max_depth`` levels deep (a scalar is
    level 0, ``[]`` level 1, ``[[]]`` level 2)."""
    stack: List[Tuple[Any, int]] = [(value, 0)]
    seen: Set[int] = set()
    while stack:
        current, depth = stack.pop()
        if isinstance(current, _CONTAINERS) and id(current) not in seen:
            if depth + 1 > max_depth:
                return True
            seen.add(id(current))
            stack.extend((child, depth + 1) for child in _children(current))
    return False


def _rebuild(
    value: Any,
    text: Callable[[str], str],
    max_depth: Optional[int],
    placeholder: Any,
    leaf: Callable[[Any], Any] = lambda other: other,
) -> Any:
    """An iterative copy of ``value``: each ``str`` (dict keys included) through ``text``, each
    other leaf through ``leaf``, and each container that would sit deeper than ``max_depth``
    replaced by ``placeholder``.

    Containers keep their type. The walk is post-order with an explicit stack: entering a
    container pushes its EXIT marker and then its members, so every member is copied before
    the container is finished -- which is what lets an immutable tuple be built as a list and
    frozen on exit. A container met again after it is finished (a shared reference) reuses
    its copy; one met again while still open is a cycle and gets the unfinished copy.
    """
    holder: List[Any] = [None]
    # ("enter", source, slot owner, slot key, depth) | ("exit", source, slot owner, slot key, copy)
    stack: List[Tuple[str, Any, Any, Any, Any]] = [("enter", value, holder, 0, 0)]
    copies: Dict[int, Any] = {}
    while stack:
        step, source, owner, key, extra = stack.pop()
        if step == "exit":
            finished = tuple(extra) if isinstance(source, tuple) else extra
            copies[id(source)] = owner[key] = finished
            continue
        depth = extra
        if isinstance(source, str):
            owner[key] = text(source)
        elif not isinstance(source, _CONTAINERS):
            owner[key] = leaf(source)
        elif max_depth is not None and depth + 1 > max_depth:
            owner[key] = placeholder
        elif id(source) in copies:
            owner[key] = copies[id(source)]
        elif isinstance(source, dict):
            built: Any = {}
            owner[key] = copies[id(source)] = built
            stack.append(("exit", source, owner, key, built))
            for item_key, item in source.items():
                new_key = text(item_key) if isinstance(item_key, str) else item_key
                built[new_key] = None
                stack.append(("enter", item, built, new_key, depth + 1))
        else:
            built = [None] * len(source)
            owner[key] = copies[id(source)] = built
            stack.append(("exit", source, owner, key, built))
            stack.extend(("enter", item, built, index, depth + 1) for index, item in enumerate(source))
    return holder[0]


def scrub_unencodable_text(value: Any) -> Any:
    """A copy of ``value`` with every unencodable string (dict keys included) passed through
    :func:`replace_unencodable`. Containers keep their type; anything else is returned as is,
    so a structure with nothing to replace comes back equal to the original."""
    return _rebuild(value, replace_unencodable, None, None)


def make_renderable(value: Any) -> Any:
    """A copy of ``value`` a JSON response can encode: unencodable characters replaced with
    U+FFFD (dict keys included), and each non-finite float with :func:`non_finite_stand_in`."""
    return _rebuild(value, replace_unencodable, None, None, _renderable_leaf)


def cut_below_depth(value: Any, max_depth: int, placeholder: Any = REPLACEMENT_CHARACTER) -> Any:
    """A copy of ``value`` in which every container nested deeper than ``max_depth`` (see
    :func:`exceeds_depth`) is replaced by ``placeholder``."""
    return _rebuild(value, lambda text: text, max_depth, placeholder)
