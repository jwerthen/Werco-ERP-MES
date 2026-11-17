"""The app-wide 422 handler must not itself 500 on input it cannot encode.

``validation_exception_handler`` (``app/main.py``) echoes each error's ``input`` / ``loc`` /
``ctx``. A JSON body may carry an unpaired UTF-16 surrogate (``"\\ud800"``), which
``json.loads`` keeps in a ``str``; echoed back, it made the response's UTF-8 encode raise --
so ANY 422 that echoed such input became a 500, on any JSON route, authenticated or not.
``_encodable_validation_errors`` now replaces such characters with U+FFFD, wherever they sit
(dict keys included), and leaves every other error exactly as it was.

The sibling fix for undecodable raw BYTES is pinned in
``tests/api/test_error_logging_endpoint.py``.
"""

import json

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import app.main as app_main
from app.core.unicode_text import (
    contains_unencodable_text,
    is_utf8_encodable,
    replace_unencodable,
    scrub_unencodable_text,
)
from app.main import _encodable_validation_errors

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
