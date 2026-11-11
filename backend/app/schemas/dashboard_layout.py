"""A person's saved dashboard layout: ordered widget placements, never authority.

The server validates the SHAPE of a layout, not its membership. The widget catalog is a
frontend concern: the SPA and the API deploy independently, and a stored id only names a
component the bundle may or may not render -- it grants nothing, because every data
endpoint a widget reads enforces its own authorization.

Stored encoding (``dashboard_layouts.layout_json``):

* ``{}`` -- the stock default (served as ``layout: null``). Only the reset verb writes it.
* ``{"format_version": 1, "widgets": [...]}`` -- a customized layout. An empty ``widgets``
  list is a deliberately EMPTY dashboard, which is why ``widgets`` is required on input:
  ``layout: {}`` is a 422, and reset is the only way back to the default.

Input is strict (``extra='forbid'``, patterns, unique ids). Every READ of a stored row goes
through :func:`normalize_stored_layout`, which is lenient, so a row an older or newer schema
wrote can never turn a GET, a save or a reset into a 500.
"""

import logging
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.schemas.base import UTCModel

logger = logging.getLogger(__name__)

# Lowercase, starts with a letter; '.' groups ids (``kpi.open_cars``). Rejects path-like ids.
WIDGET_ID_PATTERN = r'^[a-z][a-z0-9_.]{0,63}$'
# Shape only -- panel sizes are a frontend vocabulary too, and an unknown size falls back to
# the widget's own default size in the SPA rather than dropping the widget.
WIDGET_SIZE_PATTERN = r'^[a-z][a-z_]{0,23}$'
MAX_DASHBOARD_WIDGETS = 64
DASHBOARD_LAYOUT_FORMAT_VERSION = 1


class DashboardWidgetPlacement(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=WIDGET_ID_PATTERN, description='Widget id from the SPA catalog, e.g. kpi.overdue.')
    size: str | None = Field(
        default=None,
        pattern=WIDGET_SIZE_PATTERN,
        description='Panel width (third, narrow, half, wide, two_thirds, full); null uses the widget default.',
    )


class DashboardLayoutValues(BaseModel):
    model_config = ConfigDict(extra='forbid')
    format_version: Literal[1] = 1
    # REQUIRED on purpose: an empty list is an empty dashboard, never "the default".
    widgets: list[DashboardWidgetPlacement] = Field(
        max_length=MAX_DASHBOARD_WIDGETS,
        description='Ordered widget placements; an empty list is a deliberately empty dashboard.',
    )

    @model_validator(mode='after')
    def _unique_widget_ids(self) -> 'DashboardLayoutValues':
        seen: set[str] = set()
        for placement in self.widgets:
            if placement.id in seen:
                raise ValueError(f'Widget {placement.id!r} appears more than once in the layout.')
            seen.add(placement.id)
        return self


class DashboardLayoutCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_company_id: int = Field(gt=0)
    expected_version: int = Field(ge=0)


class DashboardLayoutSave(DashboardLayoutCommand):
    layout: DashboardLayoutValues


class DashboardLayoutResponse(UTCModel):
    company_id: int
    version: int
    # null = the stock default (no row, or a row reset to the default).
    layout: DashboardLayoutValues | None
    updated_at: datetime | None = None
    can_edit: bool


def _placement_or_none(entry: Any) -> DashboardWidgetPlacement | None:
    """One stored entry on its own: a bad ``id`` drops it, a bad ``size`` only loses the size."""
    if not isinstance(entry, dict):
        return None
    try:
        return DashboardWidgetPlacement.model_validate({'id': entry.get('id'), 'size': entry.get('size')})
    except ValidationError:
        pass
    try:
        return DashboardWidgetPlacement.model_validate({'id': entry.get('id')})
    except ValidationError:
        return None


def normalize_stored_layout(raw: Any, *, row_id: int | None = None) -> DashboardLayoutValues | None:
    """The ONE lenient reader of a stored layout. ``None`` means "serve the stock default".

    * ``{}`` -> ``None`` (the reset encoding; not logged).
    * A non-dict root, a ``format_version`` other than the integer 1, or a missing / non-list
      ``widgets`` -> ``None``, logged as a warning.
    * Each entry is judged on its own: a non-dict entry or an invalid ``id`` drops the entry;
      an invalid ``size`` becomes ``None`` and the entry is kept; unknown keys are ignored.
    * Duplicates keep their first occurrence; at most ``MAX_DASHBOARD_WIDGETS`` are kept.
    * A non-empty stored list from which nothing survives -> ``None`` (the default, not an
      empty dashboard). A stored EMPTY list stays an empty dashboard.

    The result is always built through ``DashboardLayoutValues.model_validate`` -- never
    ``model_construct`` -- so it satisfies the same contract as accepted input.
    """
    if isinstance(raw, dict) and not raw:
        return None
    where = f'dashboard layout row {row_id}' if row_id is not None else 'dashboard layout'
    if not isinstance(raw, dict):
        logger.warning('Ignoring stored %s: the root is %s, not an object.', where, type(raw).__name__)
        return None
    version = raw.get('format_version')
    if isinstance(version, bool) or not isinstance(version, int) or version != DASHBOARD_LAYOUT_FORMAT_VERSION:
        logger.warning('Ignoring stored %s: unsupported format_version %r.', where, version)
        return None
    stored = raw.get('widgets')
    if not isinstance(stored, list):
        logger.warning('Ignoring stored %s: widgets is %s, not a list.', where, type(stored).__name__)
        return None
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in stored:
        placement = _placement_or_none(entry)
        if placement is None or placement.id in seen:
            continue
        seen.add(placement.id)
        kept.append(placement.model_dump(mode='json'))
        if len(kept) == MAX_DASHBOARD_WIDGETS:
            break
    if stored and not kept:
        logger.warning('Ignoring stored %s: none of its %d widget entries is valid.', where, len(stored))
        return None
    if len(kept) != len(stored):
        logger.warning('Stored %s: kept %d of %d widget entries.', where, len(kept), len(stored))
    return DashboardLayoutValues.model_validate({'format_version': DASHBOARD_LAYOUT_FORMAT_VERSION, 'widgets': kept})
