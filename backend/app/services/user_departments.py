"""The fixed Department pick list for user accounts (plan alerts_health/2 step 5).

``User.department`` was free text, and notifications pick recipients by EXACT department name
(``notification_catalog`` entries' ``departments``): a buyer typed in as "purchasing " or
"Buying" never heard about low stock. Department is now one of ``DEPARTMENTS`` -- a superset of
every department the notification catalog names (a test holds that) -- or blank.

Writers that set a department go through ``canonical_department``: the admin create and update
routes, ``/auth/register``, pending-account approval and the user CSV/XLSX import. Matching is
case- and whitespace-insensitive and stores the canonical spelling. An update that leaves a
legacy (non-matching) value UNCHANGED is allowed, so editing someone's name does not force the
correction first; ``GET /users/departments`` lists those legacy values for correction.

No migration: the column stays ``String(100)``; existing values are untouched until an Admin
corrects them.
"""

from __future__ import annotations

from typing import Optional

DEPARTMENTS: tuple[str, ...] = (
    "Purchasing",
    "Inventory",
    "Sales",
    "Quality",
    "Maintenance",
    "Shipping",
    "Production",
    "Other",
)

_BY_KEY = {name.lower(): name for name in DEPARTMENTS}

DEPARTMENT_INVALID_DETAIL = "Department must be one of: " + ", ".join(DEPARTMENTS) + " (or left blank)."


def canonical_department(value: Optional[str]) -> Optional[str]:
    """The canonical spelling of a pick-list department; ``None`` for blank / ``None``.

    Raises ``ValueError(DEPARTMENT_INVALID_DETAIL)`` for anything else."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    canonical = _BY_KEY.get(text.lower())
    if canonical is None:
        raise ValueError(DEPARTMENT_INVALID_DETAIL)
    return canonical


def is_listed_department(value: Optional[str]) -> bool:
    """True for a stored value that is blank or exactly a pick-list name (what notifications match)."""
    return value is None or value == "" or value in DEPARTMENTS


def department_for_update(value: Optional[str], current: Optional[str]) -> Optional[str]:
    """``canonical_department`` for an update, except that re-sending the CURRENT stored value
    unchanged (even a legacy one) is allowed and kept as is."""
    if value == current:
        return current
    return canonical_department(value)
