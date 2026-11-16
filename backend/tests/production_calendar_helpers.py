"""Fixtures for the production calendar (P1.2): Werco's confirmed shift rules, in memory and in the DB.

Werco (plant profile, 2026-11-13): Monday-Friday, the production day starts at 05:00
Central, a shift that runs past midnight belongs to the day it started.

| Pattern                    | 1st shift   | 2nd shift   | Planned |
|----------------------------|-------------|-------------|---------|
| Weld shop (WLD-01..WLD-06) | 05:00-15:30 | 15:30-02:00 | 600 min |
| Rest of shop (default)     | 07:30-16:00 | 15:30-00:00 | 480 min |
"""

from typing import Iterable, List, Optional

from sqlalchemy.orm import Session

from app.core.time_utils import CENTRAL_TIME_ZONE
from app.schemas.production_calendar import ProductionCalendarUpdate
from app.services.audit_service import AuditService
from app.services.production_calendar import WEEKDAYS_MASK, ProductionCalendar, ShiftDef, build_calendar
from app.services.production_calendar_service import save_production_calendar

WELD_PATTERN_ID = 1
REST_PATTERN_ID = 2

W1 = ShiftDef(
    id=11,
    code="W1",
    name="Weld 1st",
    pattern_id=WELD_PATTERN_ID,
    start_minute=300,
    end_minute=930,
    days_mask=WEEKDAYS_MASK,
    planned_minutes=600,
)
W2 = ShiftDef(
    id=12,
    code="W2",
    name="Weld 2nd",
    pattern_id=WELD_PATTERN_ID,
    start_minute=930,
    end_minute=1560,
    days_mask=WEEKDAYS_MASK,
    planned_minutes=600,
)
S1 = ShiftDef(
    id=21,
    code="S1",
    name="1st shift",
    pattern_id=REST_PATTERN_ID,
    start_minute=450,
    end_minute=960,
    days_mask=WEEKDAYS_MASK,
    planned_minutes=480,
)
S2 = ShiftDef(
    id=22,
    code="S2",
    name="2nd shift",
    pattern_id=REST_PATTERN_ID,
    start_minute=930,
    end_minute=1440,
    days_mask=WEEKDAYS_MASK,
    planned_minutes=480,
)


def werco_calendar(weld_work_center_ids: Iterable[int] = (101, 102, 103, 104, 105, 106)) -> ProductionCalendar:
    """Werco's calendar built in memory (no DB): weld WCs explicit, rest of shop default."""
    return build_calendar(
        company_id=1,
        tz=CENTRAL_TIME_ZONE,
        day_start_minute=300,
        version=1,
        patterns=[
            (WELD_PATTERN_ID, False, list(weld_work_center_ids), [W1, W2]),
            (REST_PATTERN_ID, True, [], [S1, S2]),
        ],
    )


def werco_payload(weld_work_center_ids: List[int], *, expected_version: int = 0) -> dict:
    """The exact ``PUT /production-calendar`` body for Werco (docs/API.md worked example)."""
    weekdays = [0, 1, 2, 3, 4]
    return {
        "expected_version": expected_version,
        "production_day_start": "05:00",
        "patterns": [
            {
                "name": "Weld shop",
                "is_default": False,
                "work_center_ids": list(weld_work_center_ids),
                "shifts": [
                    {
                        "code": "W1",
                        "name": "Weld 1st",
                        "start": "05:00",
                        "end": "15:30",
                        "days": weekdays,
                        "planned_minutes": 600,
                    },
                    {
                        "code": "W2",
                        "name": "Weld 2nd",
                        "start": "15:30",
                        "end": "02:00",
                        "days": weekdays,
                        "planned_minutes": 600,
                    },
                ],
            },
            {
                "name": "Rest of shop",
                "is_default": True,
                "work_center_ids": [],
                "shifts": [
                    {
                        "code": "S1",
                        "name": "1st shift",
                        "start": "07:30",
                        "end": "16:00",
                        "days": weekdays,
                        "planned_minutes": 480,
                    },
                    {
                        "code": "S2",
                        "name": "2nd shift",
                        "start": "15:30",
                        "end": "00:00",
                        "days": weekdays,
                        "planned_minutes": 480,
                    },
                ],
            },
        ],
    }


def seed_werco_calendar(db: Session, company_id: int, user_id: int, weld_work_center_ids: List[int]):
    """Save Werco's calendar through the real service (required audit included) and commit."""
    audit = AuditService(db, user=None, company_id=company_id)
    response = save_production_calendar(
        db, company_id, user_id, ProductionCalendarUpdate.model_validate(werco_payload(weld_work_center_ids)), audit
    )
    db.commit()
    return response


def shift_id_by_code(response, code: str) -> Optional[int]:
    for pattern in response.patterns:
        for shift in pattern.shifts:
            if shift.code == code:
                return shift.id
    return None
