"""The placeholder report job must not claim success.

``generate_report_task`` produces no report — nothing enqueues it and nothing
consumes its result yet — so a known report type returns an honest
``status: "not_implemented"``, never ``"completed"``. The unknown-type branch
keeps its distinct ``"unsupported"`` status so the two remain distinguishable.

The job opens (and only closes) a ``SessionLocal()``; we patch it at the module
under test so the test never depends on a real database.
"""

from unittest.mock import MagicMock

import pytest

import app.jobs.report_jobs as report_jobs
from app.jobs.report_jobs import REPORT_TYPES, generate_report_task

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _mock_session(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    session = MagicMock()
    monkeypatch.setattr(report_jobs, "SessionLocal", MagicMock(return_value=session))
    return session


async def test_unknown_report_type_is_unsupported():
    result = await generate_report_task("no_such_report")
    assert result["status"] == "unsupported"
    assert "not supported" in result["message"]
    assert result["report_type"] == "no_such_report"


@pytest.mark.parametrize("report_type", sorted(REPORT_TYPES))
async def test_known_report_type_is_not_implemented_never_completed(report_type: str):
    result = await generate_report_task(report_type, filters={"status": "open"})
    assert result["status"] == "not_implemented"
    assert result["status"] != "completed"
    assert "not implemented" in result["message"]
    assert result["report_type"] == report_type
    assert result["report_name"] == REPORT_TYPES[report_type]
    assert result["filters"] == {"status": "open"}
