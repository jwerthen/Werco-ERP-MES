"""The single source of truth for every SPA route the backend may put in a notification.

A notification's ``link`` is a RELATIVE path handed to the React SPA. It is rendered two
ways: in-app as a ``<Link to={...}>`` in the bell popover / inbox, and in email as
``FRONTEND_BASE_URL + link`` in the "Open in Werco" button. If the path does not match a
route declared in ``frontend/src/App.tsx``, the SPA falls through to its catch-all
``NotFound`` — which is mounted with NO ``Layout`` and NO ``PrivateRoute``, so the sidebar,
the top bar and the notification bell itself all disappear. To the user that does not read
as "bad link", it reads as "the app broke". That is why this module exists.

Three rules, enforced by ``backend/tests/test_notification_link_routes.py``:

1. **A link value comes from a template in this module, never an inline f-string at a call
   site.** The guard test greps ``backend/app/**`` for ``link=f"/..."`` and
   ``"link_path": f"/..."`` and fails on a hit. That is what makes the route-resolution
   test TOTAL rather than a sample of whatever someone remembered to register.
2. **The destination page must be able to HONOUR the link.** A record-bearing link (one
   carrying an id) requires the landing page to resolve that id — ideally by a by-id fetch.
   A page that filters an already-loaded, windowed array can silently miss, which is worse
   than a 404 because it looks like success. If the page cannot resolve by id, either fix
   the page or emit a link with no record id in it.
3. **When there is no honest destination, emit ``None``.** A non-navigating notification
   row is correct; a link into a page that cannot honour it is not.
4. **The RECIPIENTS must be able to reach the route.** A catalog entry's audience is its
   ``roles`` ∪ ``departments``, and a department is role-agnostic — so a Quality-role user
   can receive a Purchasing-department event. ``App.tsx``'s ``routeAccessRequirements``
   then bounces them to ``/unauthorized``, which — like ``NotFound`` — renders with no
   ``Layout``: another chrome-less dead end, just a different one. Before adding a link,
   cross-check the entry's audience in ``notification_catalog.py`` against the route's
   permission in ``App.tsx`` and the role's grants in ``frontend/src/utils/permissions.ts``.
   This is why ``inspection.failed`` (roles QUALITY, MANAGER) deliberately carries NO link:
   the QUALITY role has no ``purchasing:view``, so ``/purchasing?po=…`` would send half its
   audience to Access Denied — strictly worse than the non-navigating row it has today.

   KNOWN LIMITATION, not solved here: ``link`` is computed per EVENT while access is per
   RECIPIENT, so a mixed-audience entry cannot satisfy everyone. ``receipt.created``,
   ``receipt.voided``, ``receipt.corrected``, ``receipt.inspection_cleared``, ``po.sent``,
   ``quote.expiring`` and ``calibration.due`` all have audiences that include roles lacking the destination's
   permission; those users get Access Denied where they previously got a 404. Both are dead
   ends, so this change is lateral for them and a genuine fix for everyone else. Making it
   right means a per-recipient link, which needs a backend mirror of the frontend
   permission map — a second source of truth, and a separate decision.

Query params are the mechanism for record selection: this app has few detail routes, so
the landing pattern is an existing list page plus a param it already reads (the
``frontend/src/pages/Purchasing.tsx`` ``?po=`` handling is the reference).

``LEGACY_LINK_SHAPES`` records the shapes written to ``notifications.link`` — and mailed as
ABSOLUTE urls — before this module existed. Those emails are in mailboxes forever, and
``frontend/vercel.json`` rewrites every path to ``index.html``, so a cold click from a mail
client is resolved client-side by React Router. ``App.tsx`` therefore keeps a permanent
redirect route for each shape; the guard test asserts they still exist.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Emittable link templates. Every one of these MUST resolve against App.tsx.
# ---------------------------------------------------------------------------

WORK_ORDER_DETAIL = "/work-orders/{work_order_id}"
PART_DETAIL = "/parts/{part_id}"
PURCHASE_ORDER = "/purchasing?po={po_id}"
QUOTE = "/quotes?id={quote_id}"
QUALITY_FAI_DETAIL = "/quality?tab=fai&fai={fai_id}"
QUALITY_NCR_LIST = "/quality?tab=ncr"
QUALITY_CAR_LIST = "/quality?tab=car"
# Deliberately UNFILTERED, and this is load-bearing. `?filter=due` looks more helpful and is
# provably empty: the calibration.due cron selects `Equipment.status == ACTIVE`
# (jobs/notification_jobs.py), while `GET /calibration/equipment?status=due` filters on the
# PERSISTED status column BEFORE `update_equipment_status` recomputes it
# (api/endpoints/calibration.py) — so the very rows that trigger a notification are the rows
# that filter excludes. The unfiltered list orders by `next_calibration_date` ascending (due
# item first) AND repairs the stale status for every row it returns. See rule 2 above: a
# filtered landing that silently shows nothing is worse than the 404 it replaced.
CALIBRATION_LIST = "/calibration"
DOWNTIME_LIST = "/downtime"
INVENTORY_LIST = "/inventory"
MRP_LIST = "/mrp"
SCHEDULING_LIST = "/scheduling"
VISITOR_LOG = "/visitor-log"

ALL_LINK_TEMPLATES: tuple[str, ...] = (
    WORK_ORDER_DETAIL,
    PART_DETAIL,
    PURCHASE_ORDER,
    QUOTE,
    QUALITY_FAI_DETAIL,
    QUALITY_NCR_LIST,
    QUALITY_CAR_LIST,
    CALIBRATION_LIST,
    DOWNTIME_LIST,
    INVENTORY_LIST,
    MRP_LIST,
    SCHEDULING_LIST,
    VISITOR_LOG,
)

# Shapes written to notifications.link (and DigestQueue.event_data["link"], and mailed as
# absolute URLs) BEFORE this fix. App.tsx must keep a redirect route for each one FOREVER —
# a delivered email cannot be migrated. The guard test enforces their continued existence,
# so deleting one from App.tsx turns a backend test red.
LEGACY_LINK_SHAPES: tuple[str, ...] = (
    "/purchasing/{id}",
    "/quality/ncr/{id}",
    "/quality/fai/{id}",
    # auto_evidence_service.py emits this shape as a module_link; the same redirect covers it.
    "/quality/car/{id}",
    "/shipping/{id}",
    "/calibration/{id}",
    "/quotes/{id}",
)


# ---------------------------------------------------------------------------
# Builders for the record-bearing templates
# ---------------------------------------------------------------------------


def work_order_detail(work_order_id: int) -> str:
    """Deep link to the work-order detail page (a genuine by-id route)."""
    return WORK_ORDER_DETAIL.format(work_order_id=work_order_id)


def part_detail(part_id: int) -> str:
    """Deep link to the part detail page (a genuine by-id route)."""
    return PART_DETAIL.format(part_id=part_id)


def purchase_order(po_id: int) -> str:
    """Land on Purchasing with the PO selected. There is no PO detail route; the page
    reads ``?po=`` and falls back to a by-id fetch when the PO is outside its list window."""
    return PURCHASE_ORDER.format(po_id=po_id)


def quote(quote_id: int) -> str:
    """Land on Quotes with the quote selected (``?id=``, with a by-id fetch fallback)."""
    return QUOTE.format(quote_id=quote_id)


def quality_fai_detail(fai_id: int) -> str:
    """Land on the Quality FAI tab with the report open. ``openFaiDetail`` does a real
    ``GET /quality/fai/{id}``, so this record-bearing link can always be honoured."""
    return QUALITY_FAI_DETAIL.format(fai_id=fai_id)
