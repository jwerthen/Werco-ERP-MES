"""``/api/v1/work-order-templates`` — a named catalog of jobs the shop re-runs.

WHAT THIS ROUTER IS
-------------------
Six thin verbs over ``services/work_order_template_service``. The one that matters is
``POST /{id}/use``, and what it does is call
``work_order_duplicate_service.duplicate_work_order`` against the work order the
template points at — the SAME copy engine ``POST /work-orders/{id}/duplicate`` uses.
So the result is a new work order in ``DRAFT`` with ``PENDING`` operations, exactly as
if a planner had found last month's job and pressed Duplicate.

THE TWO PROPERTIES WORTH READING BEFORE CHANGING ANYTHING HERE
--------------------------------------------------------------
**1. A template adds a name, not authority.** The role gate on every verb is the
duplicate endpoint's own trio (ADMIN / MANAGER / SUPERVISOR). Every refusal the copy
engine raises reaches the caller untouched: a retired produced part (409), a
process-sheet family with no released revision (409, structured
``code: PROCESS_SHEET_UNAVAILABLE``), an ``IntegrityError`` on generated data (409).
None of them is caught and softened here, because a template that could route around a
gate a planner would have hit by hand is a one-click hole in the create path.

**2. The result is a DRAFT, and that is the whole point of the feature.** Import Nest
Package force-sets ``RELEASED`` (``work_orders.py`` — three separate sites), so a
freshly imported nest job is on the dispatch board before anyone has reviewed it. This
router is the draft door. ``_copy_header`` hard-codes ``DRAFT`` and
``work_order_template_service._assert_landed_as_draft`` re-checks it inside the
transaction, so a regression that ever made the copy land RELEASED rolls back instead
of shipping unreviewed work to the floor.

Note what that guarantee actually rests on. The dispatch query
(``dispatch_service.queued_operations_query``) filters on OPERATION status
(``READY`` / ``IN_PROGRESS``) and on the work order not being terminal — it does NOT
exclude ``DRAFT`` work orders. A template's output is invisible on ``/dispatch`` and in
the kiosk because its operations are born ``PENDING`` and nothing promotes a DRAFT work
order, not because the board filters DRAFT out. Do not restate it the other way in a
doc or a test.

WHAT THIS ROUTER DOES NOT DO
----------------------------
It never touches the source work order. Saving a template reads it; using a template
reads it. Its status, quantities, ties and dispatch position come through unchanged —
which is the acceptance criterion for the feature, not an implementation detail.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.api.deps import get_audit_service, get_current_company_id, get_db, require_role
from app.core.realtime import safe_broadcast
from app.core.websocket import broadcast_dashboard_update
from app.db.database import atomic_transaction
from app.models.laser_nest import LaserNest
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.models.work_order_template import WorkOrderTemplate
from app.schemas.work_order import WorkOrderDuplicateResponse, WorkOrderResponse
from app.schemas.work_order_template import (
    WorkOrderTemplateCreate,
    WorkOrderTemplateListResponse,
    WorkOrderTemplatePlan,
    WorkOrderTemplateResponse,
    WorkOrderTemplateUpdate,
    WorkOrderTemplateUseRequest,
)
from app.services import work_order_template_service as templates
from app.services.audit_service import AuditService

logger = logging.getLogger(__name__)

router = APIRouter()

# The same trio ``POST /work-orders/{id}/duplicate`` and ``POST /work-orders`` require.
# A template must not admit anyone the create path would not, and the frontend gates
# every control on ``work_orders:edit``, which maps to exactly this set — so a hidden
# button and a refused call agree.
TEMPLATE_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]


def _serialize(template: WorkOrderTemplate, plan: templates.TemplatePlanSummary) -> WorkOrderTemplateResponse:
    """One template + its LIVE plan summary, as the response schema.

    The summary is never read off the template row (it is not stored there) — it is
    computed from the source work order on every read, so the planner is shown what
    they will actually get. See ``TemplatePlanSummary``.
    """
    return WorkOrderTemplateResponse(
        id=template.id,
        name=template.name,
        notes=template.notes,
        source_work_order_id=template.source_work_order_id,
        default_quantity=template.default_quantity,
        created_at=template.created_at,
        updated_at=template.updated_at,
        created_by=template.created_by,
        plan=WorkOrderTemplatePlan(
            available=plan.available,
            unavailable_reason=plan.unavailable_reason,
            source_work_order_deleted=plan.source_work_order_deleted,
            source_work_order_number=plan.source_work_order_number,
            source_status=plan.source_status,
            work_order_type=plan.work_order_type,
            sequential_operations=plan.sequential_operations,
            priority=plan.priority,
            operation_count=plan.operation_count,
            nest_count=plan.nest_count,
            planned_runs_total=plan.planned_runs_total,
            open_material_tie_count=plan.open_material_tie_count,
            work_centers=list(plan.work_centers),
            source_quantity_ordered=plan.source_quantity_ordered,
        ),
    )


def _live_source_or_404(db: Session, work_order_id: int, company_id: int) -> WorkOrder:
    """Resolve the work order a template is being saved FROM, or 404.

    Keeps invariant 3's tombstone filter — via
    ``resolve_catalogable_work_order``, deliberately NOT the read-through
    ``resolve_source_work_order`` an existing template uses. Saving a template is
    SELECTION: a deleted job is not one the shop should be able to catalogue. That an
    already-saved template keeps working over the same tombstone is the asymmetry the
    owner asked for, not an inconsistency; see the service module docstring.

    404 and never 403 for a cross-tenant id, so the response cannot confirm that it
    exists elsewhere.
    """
    source = templates.resolve_catalogable_work_order(db, work_order_id, company_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Work order not found")
    return source


@router.get(
    "",
    response_model=WorkOrderTemplateListResponse,
    summary="List the saved work order templates",
)
def list_work_order_templates(
    search: str = Query("", description="Case-insensitive substring match on name and notes."),
    current_user: User = Depends(require_role(TEMPLATE_ROLES)),
    db: Session = Depends(get_db),
    company_id: int = Depends(get_current_company_id),
):
    """The catalog, each entry carrying a LIVE summary of what using it would produce.

    A template whose source work order has been DELETED is included, summarised in
    full, and **still usable** — ``plan.available`` stays true and
    ``plan.source_work_order_deleted`` is the disclosure. Templates must not stop
    working because somebody deleted a job (owner decision); the flag is there so the
    planner can see that the exemplar is in the archive, not so the client can disable
    anything.

    ``plan.available = false`` is reserved for a source row that could not be resolved
    at all, with ``plan.unavailable_reason`` naming the cause. Such a template is still
    not filtered out: one that silently vanishes tells the planner nothing.

    Unpaged — see ``WorkOrderTemplateListResponse``.
    """
    query = templates.live_templates_query(db, company_id)
    needle = (search or "").strip()
    if needle:
        pattern = f"%{needle}%"
        query = query.filter(WorkOrderTemplate.name.ilike(pattern) | WorkOrderTemplate.notes.ilike(pattern))
    rows = query.order_by(WorkOrderTemplate.name, WorkOrderTemplate.id).all()

    # ONE batched summary read for the whole page, not one per row — the PERF-4 N+1
    # shape this repo has fixed elsewhere.
    summaries = templates.plan_summaries_for(db, rows, company_id)
    return WorkOrderTemplateListResponse(
        templates=[
            _serialize(template, summaries.get(template.id, templates.TemplatePlanSummary())) for template in rows
        ],
        total=len(rows),
    )


@router.get(
    "/{template_id}",
    response_model=WorkOrderTemplateResponse,
    summary="Read one work order template",
)
def get_work_order_template(
    template_id: int,
    current_user: User = Depends(require_role(TEMPLATE_ROLES)),
    db: Session = Depends(get_db),
    company_id: int = Depends(get_current_company_id),
):
    """One template plus its live plan summary. **404** when it is not live in this company."""
    template = templates._live_template_or_404(db, template_id, company_id)
    return _serialize(template, templates.summary_for_one(db, template, company_id))


@router.post(
    "",
    response_model=WorkOrderTemplateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Save a work order's plan as a named template",
)
def create_work_order_template(
    payload: WorkOrderTemplateCreate,
    current_user: User = Depends(require_role(TEMPLATE_ROLES)),
    db: Session = Depends(get_db),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Point a name at a work order. **The source work order is not modified.**

    Nothing about the plan is copied here — that happens at USE time, against whatever
    the source looks like then. So there is no validity gate beyond the name: a job
    whose part is currently retired, or whose process-sheet family has no released
    revision, can still be catalogued, and the refusal lands at use time where the
    planner can see and act on the cause.

    **404** when the source work order is not live in the active company.
    **409** when another LIVE template in this company already holds the name
    (compared case-insensitively — two templates differing only in case are
    indistinguishable in a picker, and the picker is the whole feature). Deleting a
    template frees its name immediately.
    """
    source = _live_source_or_404(db, payload.source_work_order_id, company_id)

    try:
        with atomic_transaction(db):
            template = templates.create_template(
                db,
                source=source,
                name=payload.name,
                notes=payload.notes,
                default_quantity=float(payload.default_quantity) if payload.default_quantity is not None else None,
                company_id=company_id,
                user_id=current_user.id,
                audit=audit,
            )
            template_id = template.id
    except IntegrityError as exc:
        # The partial unique index is the backstop behind the service's own probe: two
        # concurrent saves of the same name both pass the probe and one loses here.
        # Nothing was committed. The message names the ONE cause this can have, and
        # does not say "retry" — a retry with the same name fails identically.
        logger.warning("Work order template create hit a constraint (company %s): %s", company_id, exc)
        raise HTTPException(
            status_code=409,
            detail="A work order template with that name already exists. Pick a different name.",
        ) from exc

    template = templates._live_template_or_404(db, template_id, company_id)
    return _serialize(template, templates.summary_for_one(db, template, company_id))


@router.put(
    "/{template_id}",
    response_model=WorkOrderTemplateResponse,
    summary="Rename a template, or edit its note / default quantity",
)
def update_work_order_template(
    template_id: int,
    payload: WorkOrderTemplateUpdate,
    current_user: User = Depends(require_role(TEMPLATE_ROLES)),
    db: Session = Depends(get_db),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Edit the label, not the plan.

    ``notes`` and ``default_quantity`` are nullable and ``null`` is MEANINGFUL —
    "clear it". ``model_fields_set`` is what keeps an omitted key distinguishable from
    an explicit ``null``, so a planner can undo a typo instead of being stuck with it.

    ``source_work_order_id`` is not editable — see ``WorkOrderTemplateUpdate``.

    **404** when the template is not live in this company. **409** on a name another
    live template holds.
    """
    template = templates._live_template_or_404(db, template_id, company_id)
    provided = payload.model_fields_set

    try:
        with atomic_transaction(db):
            templates.update_template(
                db,
                template=template,
                name=payload.name,
                notes=payload.notes,
                default_quantity=(float(payload.default_quantity) if payload.default_quantity is not None else None),
                notes_provided="notes" in provided,
                default_quantity_provided="default_quantity" in provided,
                company_id=company_id,
                audit=audit,
            )
    except IntegrityError as exc:
        logger.warning("Work order template update hit a constraint (template %s): %s", template_id, exc)
        raise HTTPException(
            status_code=409,
            detail="A work order template with that name already exists. Pick a different name.",
        ) from exc

    template = templates._live_template_or_404(db, template_id, company_id)
    return _serialize(template, templates.summary_for_one(db, template, company_id))


@router.delete(
    "/{template_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete a work order template",
)
def delete_work_order_template(
    template_id: int,
    current_user: User = Depends(require_role(TEMPLATE_ROLES)),
    db: Session = Depends(get_db),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Soft delete (invariant 3). Removes a name from a picker and nothing else.

    The work orders this template created are ordinary work orders and are untouched,
    and so is the source work order it pointed at.

    The name is released for reuse immediately — the unique index is partial
    (``WHERE NOT is_deleted``). There is no restore verb on purpose: a template holds
    no information that cannot be reproduced in one click by pressing "Save as
    template" on the same work order again, because the plan was never stored in it.

    **404** on a second delete, which is what makes the tombstone filter observable.
    """
    template = templates._live_template_or_404(db, template_id, company_id)
    name = template.name

    with atomic_transaction(db):
        templates.delete_template(db, template=template, user_id=current_user.id, audit=audit)

    return {"message": f"Work order template '{name}' deleted", "id": template_id}


@router.post(
    "/{template_id}/use",
    response_model=WorkOrderDuplicateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new DRAFT work order from a template",
)
def use_work_order_template(
    template_id: int,
    payload: WorkOrderTemplateUseRequest,
    current_user: User = Depends(require_role(TEMPLATE_ROLES)),
    db: Session = Depends(get_db),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Run the saved plan again: a new work order number, a new due date, **DRAFT**.

    The copy is performed by ``duplicate_work_order`` against the work order the
    template points at, so everything that verb decides holds here byte-for-byte: the
    header plan, operations and their instructions, live laser nests (CNC number,
    planned runs, material/thickness/sheet, the SHARED drawing reference), OPEN
    material ties with their lot pins cleared, and process-sheet steps re-snapshotted
    from each family's currently-released revision. What the last run actually did
    stays with the source: quantities, actual hours, clocks, lot/serial, unit number,
    ``parent_work_order_id``, ``must_ship_by``, dispatch rank.

    **Nothing reaches the floor.** The new work order is DRAFT and its operations are
    PENDING, so it contributes zero rows to ``/dispatch`` and to the kiosk queue until
    somebody releases it — which is the difference between this door and Import Nest
    Package, and the reason the feature exists.

    The response is the SAME envelope ``POST /work-orders/{id}/duplicate`` returns:
    ``work_order`` plus ``skipped_operations`` / ``skipped_material_allocations``. Both
    lists are normally empty; when they are not, the draft is valid but MISSING
    something the source had, and the planner has to be told — a skipped material tie
    that nobody surfaces means the job runs and stock is never deducted.

    **A soft-deleted source work order is used normally** — that refusal was removed by
    owner decision, because a template is a catalog entry and must not stop working
    because somebody deleted a job. The copy is unaffected: ``duplicate_work_order``
    never asked whether the source was deleted, it copies the object it is handed.

    **404** when the template is not live in this company.
    **409** when the source work order row cannot be resolved at all (near-unreachable:
    ``source_work_order_id`` is NOT NULL with an FK, and the hard-delete verb refuses
    rather than orphaning a template), when the source's produced part has been
    retired, when an operation's process-sheet family has no released revision
    (structured detail, ``code: PROCESS_SHEET_UNAVAILABLE``), or on a constraint fault
    in the generated data. Nothing is written in any of those cases.
    **422** when no positive quantity can be resolved from the request, the template's
    default, or the source work order.
    """
    template = templates._live_template_or_404(db, template_id, company_id)

    try:
        # ONE unit of work: the new work order, its operations, nest package, nests,
        # material ties, the template's USE audit row and every row the copy writes
        # commit together or not at all. A header without its nests is a plan nobody
        # approved.
        with atomic_transaction(db):
            result = templates.use_template(
                db,
                template=template,
                quantity_ordered=(float(payload.quantity_ordered) if payload.quantity_ordered is not None else None),
                due_date=payload.due_date,
                company_id=company_id,
                user_id=current_user.id,
                audit=audit,
            )
            new_work_order_id = result.duplicate.work_order.id
            # Read the skips off the result INSIDE the block: the objects survive the
            # commit, but these lists are the only record of them outside the chain.
            skipped_operations = list(result.duplicate.skipped_operations)
            skipped_material_allocations = list(result.duplicate.skipped_allocations)
    except IntegrityError as exc:
        # Same handling and the same reasoning as the duplicate endpoint: a
        # uniqueness/constraint fault must not surface as a 500 on a poisoned session,
        # and the message deliberately does NOT say "retry" — only the work-order-number
        # race is transient; the rest are properties of the source and fail identically
        # every time.
        logger.warning("Work order template use failed on a constraint error (template %s): %s", template_id, exc)
        raise HTTPException(
            status_code=409,
            detail="Could not create a work order from this template; a generated record conflicts with an "
            "existing one. If it fails the same way again, the work order this template was saved from has "
            "data that cannot be copied — check its nests and material ties.",
        ) from exc

    work_order = (
        db.query(WorkOrder)
        .options(
            joinedload(WorkOrder.part),
            selectinload(WorkOrder.operations).selectinload(WorkOrderOperation.component_part),
            selectinload(WorkOrder.operations).selectinload(WorkOrderOperation.work_center),
            selectinload(WorkOrder.operations)
            .selectinload(WorkOrderOperation.laser_nest)
            .selectinload(LaserNest.document),
        )
        .filter(WorkOrder.id == new_work_order_id, WorkOrder.company_id == company_id)
        .first()
    )
    _enrich(work_order, db=db, company_id=company_id)

    # The same broadcast ``POST /work-orders`` and ``POST /work-orders/{id}/duplicate``
    # emit. Without it a template-created draft is invisible on every OTHER open
    # session's Work Orders list until someone reloads, while a duplicate appears --
    # an unexplained divergence in a pair that is otherwise deliberately identical.
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "work_order_created",
            "work_order_id": work_order.id,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        },
        company_id=company_id,
    )

    return WorkOrderDuplicateResponse(
        # ``model_validate`` rather than handing the ORM object straight in, matching
        # the duplicate endpoint line-for-line -- the two "identical envelope" paths
        # should read identically, not merely behave the same via from_attributes.
        work_order=WorkOrderResponse.model_validate(work_order),
        skipped_operations=skipped_operations,
        skipped_material_allocations=skipped_material_allocations,
    )


def _enrich(work_order, *, db: Session, company_id: int) -> None:
    """Apply the work-orders router's own response enrichment to the created draft.

    ``db``/``company_id`` are forwarded because the enrichment now also serves hold
    provenance (why an operation is held, by whom, when) and that read is tenant-scoped.
    ``company_id`` is the ACTIVE company the endpoint already resolved, never client input.
    A template always produces a DRAFT with PENDING operations, so nothing here is ever on
    hold and the lookup short-circuits without a query -- forwarded anyway so this path
    cannot silently diverge from the duplicate endpoint it is required to mirror.

    Imported lazily: ``work_orders.py`` imports the duplicate service at load time and
    this module's service imports it too, so a module-scope import here would risk
    closing a cycle for a single call. Same precedent as
    ``work_order_duplicate_service`` importing ``generate_work_order_number`` inside
    the function.

    Reused rather than reimplemented so a template's response is byte-identical to the
    duplicate endpoint's — the frontend renders both through the same result view.
    """
    from app.api.endpoints.work_orders import _enrich_work_order_operations

    _enrich_work_order_operations(work_order, db=db, company_id=company_id)
