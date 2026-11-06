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
