"""The app-wide 422 handler must not itself 500 on input it cannot encode.

``validation_exception_handler`` (``app/main.py``) echoes each error's ``input`` / ``loc`` /
``ctx``. A JSON body may carry an unpaired UTF-16 surrogate (``"\\ud800"``), which
``json.loads`` keeps in a ``str``; echoed back, it made the response's UTF-8 encode raise --
so ANY 422 that echoed such input became a 500, on any JSON route, authenticated or not.
``_encodable_validation_errors`` now replaces such characters with U+FFFD, wherever they sit
(dict keys included), and leaves every other error exactly as it was.

Two more ways the same 422 became a 500, pinned here too:

* ``NaN`` / ``Infinity`` / ``-Infinity``: ``json.loads`` accepts the literals, and the JSON
  response refuses to encode the floats ("Out of range float values are not JSON
  compliant"). They are echoed back as the strings ``"NaN"`` / ``"Infinity"`` / ``"-Infinity"``.
* Depth: the first scrub walked the error recursively, so a ``[[[...]]]`` body a few hundred
  levels deep (under 1 KB, no surrogate needed) raised ``RecursionError`` -- a 500, an ERROR
  line and a Sentry event per unauthenticated request. The walks are iterative now, and a
  scrub that raises anyway falls back to passing the error through.

The sibling fix for undecodable raw BYTES is pinned in
``tests/api/test_error_logging_endpoint.py``.
"""

import json
import sys

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import app.main as app_main
from app.core.unicode_text import (
    contains_unencodable_text,
    contains_unrenderable,
    cut_below_depth,
    exceeds_depth,
    is_utf8_encodable,
    make_renderable,
    replace_unencodable,
    scrub_unencodable_text,
)
from app.main import _encodable_validation_errors

REFRESH = "/api/v1/auth/refresh"

LONE = "\ud800"
LOW = "\udfff"
FFFD = "�"


def _render(errors) -> bytes:
    """What the handler does with the errors: encode, then build the JSON response body."""
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)}).body


def test_an_unencodable_echo_would_fail_the_response_and_is_now_rendered():
    errors = [
        {
            "type": "extra_forbidden",
            "loc": ("body", "columns", 0, f"k{LONE}"),
            "msg": "Extra inputs are not permitted",
            "input": {"field": f"x{LOW}", f"k{LONE}": [1, f"{LONE}{LOW}"], "n": 3, "ok": None},
            "ctx": {"problems": [f"p{LONE}"], "nested": ({"deep": LOW},)},
        }
    ]
    with pytest.raises(UnicodeEncodeError):
        _render(errors)  # the hazard is real: this is the 500 the handler used to answer

    [safe] = _encodable_validation_errors(errors)
    assert safe["loc"] == ("body", "columns", 0, f"k{FFFD}")
    assert safe["input"] == {"field": f"x{FFFD}", f"k{FFFD}": [1, f"{FFFD}{FFFD}"], "n": 3, "ok": None}
    assert safe["ctx"] == {"problems": [f"p{FFFD}"], "nested": ({"deep": FFFD},)}
    assert safe["type"] == "extra_forbidden" and safe["msg"] == "Extra inputs are not permitted"
    json.loads(_render([safe]))


def test_an_error_with_nothing_to_replace_is_passed_through_untouched():
    """Everything else about the handler is unchanged: the same object, not even a copy."""
    marker = ValueError("not json")  # ctx may hold an exception object; it is not looked into
    error = {
        "type": "value_error",
        "loc": ("body", "name"),
        "msg": "Value error, x",
        "input": "café",
        "ctx": {"error": marker},
    }
    [out] = _encodable_validation_errors([error])
    assert out is error


def test_undecodable_bytes_are_still_decoded_with_replacement():
    [out] = _encodable_validation_errors([{"type": "json_invalid", "loc": ("body",), "msg": "m", "input": b"\xff{"}])
    assert out["input"] == "�{"


@pytest.mark.parametrize("body", [[LONE], {"refresh_token": [f"k{LONE}", LOW]}], ids=["list body", "field value"])
def test_a_422_echoing_a_lone_surrogate_is_a_real_422(body):
    """End to end on an unauthenticated JSON route: the body fails validation, the 422 echoes
    it, and the response is a 422 with U+FFFD in place of each surrogate -- not a 500."""
    client = TestClient(app_main.app, raise_server_exceptions=False)
    response = client.post(
        "/api/v1/auth/refresh",
        content=json.dumps(body).encode("ascii"),  # the surrogate travels as the \\ud800 escape
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422, response.text
    response.content.decode("utf-8")
    echoed = json.dumps(response.json()["detail"], ensure_ascii=False)
    assert LONE not in echoed and LOW not in echoed
    assert FFFD in echoed


def test_unicode_text_helpers():
    assert is_utf8_encodable("plain café ☃ 😀") and not is_utf8_encodable(f"a{LONE}")
    assert replace_unencodable(f"a{LONE}b{LOW}c") == f"a{FFFD}b{FFFD}c"
    assert replace_unencodable("unchanged ☃") == "unchanged ☃"
    assert contains_unencodable_text({"k": [1, {"x": (None, LOW)}]})
    assert contains_unencodable_text({LONE: 1})
    assert not contains_unencodable_text({"k": [1, 2.5, None, True, "☃"]})
    assert scrub_unencodable_text([("a", LONE)]) == [("a", FFFD)]


# ===========================================================================
# Non-finite numbers
# ===========================================================================


def test_a_non_finite_echo_would_fail_the_response_and_is_now_rendered():
    nan, inf = float("nan"), float("inf")
    errors = [{"type": "string_type", "loc": ("body", "x"), "msg": "m", "input": {"a": [nan, -inf], "b": (inf,)}}]
    with pytest.raises(ValueError, match="not JSON compliant"):
        _render(errors)  # the 500 the handler used to answer
    [safe] = _encodable_validation_errors(errors)
    assert safe["input"] == {"a": ["NaN", "-Infinity"], "b": ("Infinity",)}
    json.loads(_render([safe]))


@pytest.mark.parametrize(
    "body", ["NaN", "[1, -Infinity]", '{"refresh_token": Infinity}'], ids=["NaN", "-Infinity", "Infinity"]
)
def test_a_422_echoing_a_non_finite_number_is_a_real_422(body: str):
    client = TestClient(app_main.app, raise_server_exceptions=False)
    response = client.post(REFRESH, content=body.encode("ascii"), headers={"content-type": "application/json"})
    assert response.status_code == 422, response.text
    assert any(token in response.text for token in ('"NaN"', '"Infinity"', '"-Infinity"')), response.text


def test_a_nan_in_an_authenticated_create_is_a_422(client: TestClient, auth_headers: dict):
    """App-wide, not a report-builder quirk: a part create whose body carries a NaN."""
    response = client.post(
        "/api/v1/parts/",
        content=b'{"safety_stock": NaN}',
        headers={**auth_headers, "content-type": "application/json"},
    )
    assert response.status_code == 422, response.text
    assert '"NaN"' in response.text


# ===========================================================================
# Depth: the walks are iterative, and a failing scrub never becomes a 500
# ===========================================================================


@pytest.mark.parametrize("leaf", ["1", '"\\ud800"', "NaN"], ids=["plain", "surrogate leaf", "NaN leaf"])
def test_a_body_nested_900_deep_is_a_422_not_a_500(leaf: str):
    """Before the first scrub this was a 422 up to about 950 levels; the recursive scrub made it
    a 500 from about 350. The response echoes the whole body, so it is not parsed here (the
    test's own json.loads would recurse)."""
    client = TestClient(app_main.app, raise_server_exceptions=False)
    body = "[" * 900 + leaf + "]" * 900
    response = client.post(REFRESH, content=body.encode("ascii"), headers={"content-type": "application/json"})
    assert response.status_code == 422, response.text[:300]
    response.content.decode("utf-8")
    if "ud800" in leaf:
        assert FFFD.encode("utf-8") in response.content


def test_a_scrub_that_raises_falls_back_to_the_error_as_it_was(monkeypatch):
    def broken(_value):
        raise RuntimeError("a defect in the scrub")

    monkeypatch.setattr(app_main, "contains_unrenderable", broken)
    error = {"type": "string_type", "loc": ("body",), "msg": "m", "input": [1, 2]}
    assert _encodable_validation_errors([error]) == [error]


def test_the_walkers_are_iterative():
    """100,000 levels under a recursion limit of 1,000: any recursive walk would raise."""
    deep = [LONE]
    for _ in range(100_000):
        deep = [deep]
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(1_000)
    try:
        assert contains_unencodable_text(deep) and contains_unrenderable(deep)
        assert exceeds_depth(deep, 32) and not exceeds_depth([[1]], 2)
        scrubbed, rendered = scrub_unencodable_text(deep), make_renderable(deep)
        cut = cut_below_depth(deep, 3)
    finally:
        sys.setrecursionlimit(old_limit)
    for copy in (scrubbed, rendered):
        for _ in range(100_001):
            copy = copy[0]
        assert copy == FFFD
    assert cut == [[[FFFD]]]


def test_render_helpers_keep_types_and_shared_members():
    shared = ("x", LONE)
    rendered = make_renderable({"a": [shared, (shared, float("-inf"))], "b": shared, "n": 1, "t": True})
    assert rendered == {"a": [("x", FFFD), (("x", FFFD), "-Infinity")], "b": ("x", FFFD), "n": 1, "t": True}
    assert rendered["b"] is rendered["a"][0]
    assert not contains_unrenderable({"k": [1, 2.5, None, True, "\u2603"]})
