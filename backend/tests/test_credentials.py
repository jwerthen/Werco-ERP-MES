"""Unit tests for app.core.credentials (pure predicates, no database)."""

from types import SimpleNamespace

import pytest

from app.api import deps
from app.core import credentials
from app.core.credentials import is_api_token_credential, is_interactive_person

pytestmark = pytest.mark.unit


def test_api_token_scope_matches_deps():
    # credentials.py repeats the constant to stay import-cycle free; keep them equal.
    assert credentials.API_TOKEN_SCOPE == deps.API_TOKEN_SCOPE


def test_interactive_session_is_a_person():
    user = SimpleNamespace(_api_token_id=None, _token_scope=None)
    assert is_api_token_credential(user) is False
    assert is_interactive_person(user) is True


def test_api_token_is_neither():
    user = SimpleNamespace(_api_token_id=7, _token_scope="api")
    assert is_api_token_credential(user) is True
    assert is_interactive_person(user) is False


def test_api_token_detected_by_row_id_even_without_scope():
    user = SimpleNamespace(_api_token_id=7, _token_scope=None)
    assert is_api_token_credential(user) is True
    assert is_interactive_person(user) is False


def test_crew_badge_token_is_not_an_api_token_but_not_interactive():
    user = SimpleNamespace(_api_token_id=None, _token_scope="kiosk")
    assert is_api_token_credential(user) is False
    assert is_interactive_person(user) is False


def test_user_built_outside_a_request_reads_as_interactive():
    user = SimpleNamespace()
    assert is_api_token_credential(user) is False
    assert is_interactive_person(user) is True


# --------------------------------------------------------------------------- migration 119 narrowing


def test_scope_constants_match_the_model_enum_and_the_jwt_minter():
    # credentials.py and security.py repeat the scope values to stay import-free; keep all three equal.
    from app.core import security
    from app.models.api_token import ApiTokenScope

    assert credentials.API_TOKEN_SCOPE_FULL == ApiTokenScope.FULL.value == security._API_SCOPE_FULL
    assert credentials.API_TOKEN_SCOPE_READ_ONLY == ApiTokenScope.READ_ONLY.value
    assert {member.value for member in ApiTokenScope} == {
        credentials.API_TOKEN_SCOPE_FULL,
        credentials.API_TOKEN_SCOPE_READ_ONLY,
    }


def test_the_refusal_texts_are_the_documented_ones():
    assert credentials.READ_ONLY_ROUTE_WRITES_DETAIL == "Read-only API token cannot modify data."
    assert credentials.READ_ONLY_ROUTE_DENIED_DETAIL == "Read-only API token cannot access this resource."
    assert credentials.READ_ONLY_TOOL_WRITES_DETAIL == "Read-only API token: this tool writes."
    assert (
        credentials.READ_ONLY_TOOL_DENIED_DETAIL
        == "Read-only API token: this tool is not available to read-only tokens."
    )


@pytest.mark.parametrize(
    "scope,expected",
    [
        ("full", False),
        ("read_only", True),
        # Fail closed: anything but exactly "full" on an API token is read-only.
        (None, True),
        ("FULL", True),
        ("", True),
    ],
)
def test_is_read_only_api_token_fails_closed(scope, expected):
    user = SimpleNamespace(_api_token_id=7, _token_scope="api", _api_token_scope=scope)
    assert credentials.is_read_only_api_token(user) is expected


def test_a_token_with_no_scope_marker_at_all_is_read_only():
    assert credentials.is_read_only_api_token(SimpleNamespace(_api_token_id=7)) is True


@pytest.mark.parametrize(
    "marker,expected",
    [
        (False, False),
        (True, True),
        # Fail closed: anything but exactly False on an API token is AI-external.
        (None, True),
        (0, True),
        ("false", True),
    ],
)
def test_is_ai_external_credential_fails_closed(marker, expected):
    user = SimpleNamespace(_api_token_id=7, _token_scope="api", _api_token_ai_external=marker)
    assert credentials.is_ai_external_credential(user) is expected


def test_a_token_with_no_ai_external_marker_at_all_is_ai_external():
    assert credentials.is_ai_external_credential(SimpleNamespace(_api_token_id=7)) is True


@pytest.mark.parametrize(
    "user",
    [
        SimpleNamespace(
            _api_token_id=None, _token_scope=None, _api_token_scope="read_only", _api_token_ai_external=True
        ),
        SimpleNamespace(_api_token_id=None, _token_scope="kiosk", _api_token_scope=None, _api_token_ai_external=None),
        SimpleNamespace(),
    ],
    ids=["person-with-stale-markers", "crew-badge", "bare-user"],
)
def test_a_non_token_credential_is_never_narrowed(user):
    """A person (or a badge) never inherits a token's narrowing, whatever stale markers say."""
    assert credentials.is_read_only_api_token(user) is False
    assert credentials.is_ai_external_credential(user) is False
    assert credentials.is_narrowed_api_token(user) is False


@pytest.mark.parametrize(
    "scope,ai_external,expected",
    [
        ("full", False, False),
        ("read_only", False, True),
        ("read_only", True, True),
        # A tampered full + AI-external row is still narrowed (AI-external is read-only whatever
        # its scope says), and every missing marker fails closed.
        ("full", True, True),
        (None, False, True),
        ("full", None, True),
    ],
)
def test_is_narrowed_api_token_is_the_union_and_fails_closed(scope, ai_external, expected):
    user = SimpleNamespace(
        _api_token_id=7, _token_scope="api", _api_token_scope=scope, _api_token_ai_external=ai_external
    )
    assert credentials.is_narrowed_api_token(user) is expected


def test_the_egress_switch_refusal_text_is_the_documented_one():
    assert credentials.API_TOKEN_EGRESS_SWITCH == (
        "A company egress kill switch (AI, SMS, carrier or label-print egress) is turned on or off by an Admin "
        "signed in to the app, not an API token: opening or closing data egress is a person's judgment."
    )


def test_the_comment_write_refusal_text_is_the_documented_one():
    assert credentials.API_TOKEN_COMMENT_WRITE == (
        "A comment is a person's own words: an API token cannot post, edit or delete comments or change who is "
        "watching a record. Sign in to the app."
    )
