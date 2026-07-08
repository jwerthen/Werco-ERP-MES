"""Estimate workbench API — Phase 1–6: recalc, CRUD, verification, finalize, PDF, shop data, export.

POST /estimate-workbench/recalc
GET  /estimate-workbench/shop-data
PATCH /estimate-workbench/shop-data/{kind}/rows/{row_id}
POST /estimate-workbench/shop-data/{kind}/rows
GET  /estimate-workbench/shop-data/history
GET  /estimate-workbench/job-actuals
POST /estimate-workbench/job-actuals
POST /estimate-workbench/
GET  /estimate-workbench/{estimate_id}
PUT  /estimate-workbench/{estimate_id}
GET  /estimate-workbench/{estimate_id}/verification
POST /estimate-workbench/{estimate_id}/finalize
POST /estimate-workbench/{estimate_id}/extract-from-rfq
GET  /estimate-workbench/{estimate_id}/export/audit.xlsx
GET  /estimate-workbench/{estimate_id}/export/audit.json
GET  /estimate-workbench/{estimate_id}/export/customer.pdf
"""

from __future__ import annotations

from io import BytesIO
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, require_role
from app.db.database import get_db
from app.models.rfq_quote import RfqPackage
from app.models.user import User, UserRole
from app.schemas.estimate_workbench import (
    AssemblyDraftOut,
    AssemblyOut,
    BidSummaryOut,
    BuyoutLineDraftOut,
    BuyoutLineOut,
    CalcMessageOut,
    CutBendRowCreateRequest,
    CutBendRowOut,
    CutBendRowUpdateRequest,
    CutBendTableOut,
    ExtractFromRfqRequest,
    ExtractFromRfqResponse,
    ExtractionSummaryOut,
    FabLineDraftOut,
    FabLineOut,
    FabLineRecalcOut,
    FinalizeRequest,
    FinalizeResponse,
    JobActualOut,
    JobActualUpsertRequest,
    MachinedLineOut,
    MachinedLineRecalcOut,
    RecalcRequest,
    RecalcResponse,
    ShopDataHistoryItemOut,
    ShopDataTablesResponse,
    VerificationReportOut,
    WorkbenchCreateRequest,
    WorkbenchResponse,
    WorkbenchSaveRequest,
)
from app.services.audit_service import AuditService
from app.services.estimate_workbench_export_service import (
    ExportBlockedError,
    build_workbench_audit_json_bytes,
    build_workbench_audit_xlsx,
    build_workbench_customer_pdf,
)
from app.services.estimate_workbench_extraction_service import (
    ExtractionError,
    apply_extraction_to_estimate,
    extract_workbench_draft_from_rfq,
)
from app.services.estimate_workbench_service import (
    FinalizeBlockedError,
    StaleVersionError,
    build_verification_report,
    create_blank_estimate,
    finalize_estimate,
    get_estimate_tree,
    load_shop_data,
    recalc_payload,
    save_estimate_tree,
)
from app.services.shop_data_service import (
    ShopDataError,
    create_shop_data_row,
    list_job_actuals,
    list_shop_data_history,
    list_shop_data_tables,
    update_shop_data_row,
    upsert_job_actual,
)

router = APIRouter()

_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]


def _msg_from_error(err) -> CalcMessageOut:
    return CalcMessageOut(
        code=err.code.value if hasattr(err.code, "value") else str(err.code),
        message=err.message,
        field=err.field,
    )


def _msg_from_warning(warn) -> CalcMessageOut:
    return CalcMessageOut(
        code=warn.code,
        message=warn.message,
        field=warn.field,
        suggested_value=warn.suggested_value,
    )


def _serialize_tree(estimate, shop_data_source: Optional[str] = None) -> WorkbenchResponse:
    assemblies: List[AssemblyOut] = []
    for asm in estimate.assemblies or []:
        if getattr(asm, "is_deleted", False):
            continue
        fab_lines = [
            FabLineOut.model_validate(fl)
            for fl in (asm.fab_line_items or [])
            if not getattr(fl, "is_deleted", False)
        ]
        buyout_lines = [
            BuyoutLineOut.model_validate(bl)
            for bl in (asm.buyout_line_items or [])
            if not getattr(bl, "is_deleted", False)
        ]
        assemblies.append(
            AssemblyOut(
                id=asm.id,
                name=asm.name,
                sort_order=asm.sort_order,
                assembly_labor_hrs=asm.assembly_labor_hrs,
                electrical_labor_hrs=asm.electrical_labor_hrs,
                notes=asm.notes,
                version=asm.version,
                fab_lines=fab_lines,
                buyout_lines=buyout_lines,
            )
        )

    machined = [
        MachinedLineOut.model_validate(mp)
        for mp in (estimate.machined_line_items or [])
        if not getattr(mp, "is_deleted", False)
    ]

    breakdown = estimate.internal_breakdown or {}
    verification = VerificationReportOut.model_validate(build_verification_report(estimate))
    return WorkbenchResponse(
        estimate_id=estimate.id,
        rfq_package_id=estimate.rfq_package_id,
        quote_id=estimate.quote_id,
        version=int(estimate.version or 1),
        currency=estimate.currency or "USD",
        grand_total=float(estimate.grand_total or 0),
        material_total=float(estimate.material_total or 0),
        hardware_consumables_total=float(estimate.hardware_consumables_total or 0),
        shop_labor_oh_total=float(estimate.shop_labor_oh_total or 0),
        margin_total=float(estimate.margin_total or 0),
        internal_breakdown=breakdown,
        assemblies=assemblies,
        machined_parts=machined,
        shop_data_source=shop_data_source or breakdown.get("shop_data_source"),
        verification=verification,
    )


@router.post("/recalc", response_model=RecalcResponse)
def recalc_estimate(
    body: RecalcRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Stateless recalculation using company Cut/Bend tables + rates."""
    _ = current_user
    result = recalc_payload(
        db,
        company_id,
        body.assemblies,
        body.machined_parts,
        rates_override=body.rates,
    )
    summary = result["summary"]
    fab_outs = [
        FabLineRecalcOut(
            detail_name=item["detail_name"],
            part_number=item["part_number"],
            material_family=item["breakdown"].material_family.value,
            weight_ea_lb=item["breakdown"].weight_ea_lb,
            material_cost=item["breakdown"].material_cost,
            laser_cost=item["breakdown"].laser_cost,
            laser_hours=item["breakdown"].laser_hours,
            brake_cost=item["breakdown"].brake_cost,
            brake_hours=item["breakdown"].brake_hours,
            weld_cost=item["breakdown"].weld_cost,
            weld_hours=item["breakdown"].weld_hours,
            weld_minutes_ea=item["breakdown"].weld_minutes_ea,
            line_total=item["breakdown"].line_total,
            cut_length_used=item["breakdown"].cut_length_used,
            errors=[_msg_from_error(e) for e in item["breakdown"].errors],
            warnings=[_msg_from_warning(w) for w in item["breakdown"].warnings],
        )
        for item in result["fab_outs"]
    ]
    machined_outs = [
        MachinedLineRecalcOut(
            description=item.get("description"),
            weight_ea_lb=item["weight_ea_lb"],
            material_cost=item["material_cost"],
            turning_cost=item["turning_cost"],
            turning_hours=item["turning_hours"],
            milling_cost=item["milling_cost"],
            milling_hours=item["milling_hours"],
            line_total=item["line_total"],
        )
        for item in result["machined_outs"]
    ]
    return RecalcResponse(
        fab_lines=fab_outs,
        machined_parts=machined_outs,
        shop_data_source=result["shop_data_source"],
        bid_summary=BidSummaryOut(
            fab_material=summary.fab_material,
            fab_laser=summary.fab_laser,
            fab_brake=summary.fab_brake,
            fab_weld=summary.fab_weld,
            fab_subtotal=summary.fab_subtotal,
            buyout_subtotal=summary.buyout_subtotal,
            buyout_marked_up=summary.buyout_marked_up,
            assembly_labor_cost=summary.assembly_labor_cost,
            electrical_labor_cost=summary.electrical_labor_cost,
            machined_subtotal=summary.machined_subtotal,
            laser_hours=summary.laser_hours,
            brake_hours=summary.brake_hours,
            weld_hours=summary.weld_hours,
            assembly_hours=summary.assembly_hours,
            electrical_hours=summary.electrical_hours,
            subtotal_before_oh=summary.subtotal_before_oh,
            overhead=summary.overhead,
            consumables=summary.consumables,
            cogs=summary.cogs,
            sell_price=summary.sell_price,
            target_margin=summary.target_margin,
            errors=[_msg_from_error(e) for e in summary.errors],
        ),
    )


# ---------------------------------------------------------------------------
# Phase 5 — Shop Data (static paths BEFORE /{estimate_id})
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


@router.get("/shop-data", response_model=ShopDataTablesResponse)
def get_shop_data(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """List the five Cut/Bend lookup tables (auto-seeds Excel defaults if empty)."""
    _ = current_user
    tables = list_shop_data_tables(db, company_id)
    return ShopDataTablesResponse(
        tables=[CutBendTableOut.model_validate(t) for t in tables],
        source="db" if tables else "defaults",
    )


@router.get("/shop-data/history", response_model=List[ShopDataHistoryItemOut])
def get_shop_data_history(
    kind: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    _ = current_user
    items = list_shop_data_history(db, company_id, kind=kind, limit=limit)
    return [ShopDataHistoryItemOut.model_validate(i) for i in items]


@router.patch("/shop-data/{kind}/rows/{row_id}", response_model=CutBendRowOut)
def patch_shop_data_row(
    kind: str,
    row_id: int,
    body: CutBendRowUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Edit one Cut/Bend cell/row. Requires a change note (AS9100 audit)."""
    updates = body.model_dump(exclude={"note"}, exclude_unset=True)
    try:
        row = update_shop_data_row(
            db,
            company_id=company_id,
            kind=kind,
            row_id=row_id,
            updates=updates,
            note=body.note,
            current_user=current_user,
            ip_address=_client_ip(request),
        )
    except ShopDataError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return CutBendRowOut.model_validate(row)


@router.post("/shop-data/{kind}/rows", response_model=CutBendRowOut, status_code=201)
def post_shop_data_row(
    kind: str,
    body: CutBendRowCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Add a thickness / gauge / fillet band. Auto-sorts for banded lookup."""
    payload = body.model_dump(exclude={"note"})
    try:
        row = create_shop_data_row(
            db,
            company_id=company_id,
            kind=kind,
            payload=payload,
            note=body.note,
            current_user=current_user,
            ip_address=_client_ip(request),
        )
    except ShopDataError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return CutBendRowOut.model_validate(row)


@router.get("/job-actuals", response_model=List[JobActualOut])
def get_job_actuals(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Quoted vs actual hours for Shop Data tuning prompts."""
    _ = current_user
    return [JobActualOut.model_validate(r) for r in list_job_actuals(db, company_id, limit=limit)]


@router.post("/job-actuals", response_model=JobActualOut)
def post_job_actual(
    body: JobActualUpsertRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Enter or update post-job actual laser/brake/weld hours."""
    try:
        row = upsert_job_actual(
            db,
            company_id=company_id,
            user_id=current_user.id,
            quote_estimate_id=body.quote_estimate_id,
            work_order_id=body.work_order_id,
            job_label=body.job_label,
            actual_laser_hours=body.actual_laser_hours,
            actual_brake_hours=body.actual_brake_hours,
            actual_weld_hours=body.actual_weld_hours,
            notes=body.notes,
            quoted_laser_hours=body.quoted_laser_hours,
            quoted_brake_hours=body.quoted_brake_hours,
            quoted_weld_hours=body.quoted_weld_hours,
        )
    except ShopDataError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return JobActualOut.model_validate(row)


@router.post("/", response_model=WorkbenchResponse, status_code=201)
def create_workbench(
    body: WorkbenchCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a blank estimate workbench attached to an RFQ package."""
    _ = request
    try:
        estimate = create_blank_estimate(
            db,
            rfq_package_id=body.rfq_package_id,
            company_id=company_id,
            user_id=current_user.id,
            audit=audit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    shop = load_shop_data(db, company_id)
    return _serialize_tree(estimate, shop_data_source=shop.source)


@router.get("/{estimate_id}", response_model=WorkbenchResponse)
def get_workbench(
    estimate_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    _ = current_user
    estimate = get_estimate_tree(db, estimate_id, company_id)
    if not estimate:
        raise HTTPException(status_code=404, detail="Estimate not found")
    shop = load_shop_data(db, company_id)
    return _serialize_tree(estimate, shop_data_source=shop.source)


@router.put("/{estimate_id}", response_model=WorkbenchResponse)
def save_workbench(
    estimate_id: int,
    body: WorkbenchSaveRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Replace workbench tree and recompute. Requires matching ``version``."""
    _ = request
    estimate = get_estimate_tree(db, estimate_id, company_id)
    if not estimate:
        raise HTTPException(status_code=404, detail="Estimate not found")
    payload = body.model_dump()
    try:
        saved = save_estimate_tree(
            db,
            estimate,
            payload,
            expected_version=body.version,
            company_id=company_id,
            user_id=current_user.id,
            audit=audit,
        )
    except StaleVersionError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Estimate changed since you loaded it — refresh and merge",
                "current_version": exc.current_version,
            },
        ) from exc
    shop = load_shop_data(db, company_id)
    return _serialize_tree(saved, shop_data_source=shop.source)


@router.get("/{estimate_id}/verification", response_model=VerificationReportOut)
def get_verification(
    estimate_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Bid Verification dashboard — category counts + Priority Action Items."""
    _ = current_user
    estimate = get_estimate_tree(db, estimate_id, company_id)
    if not estimate:
        raise HTTPException(status_code=404, detail="Estimate not found")
    return VerificationReportOut.model_validate(build_verification_report(estimate))


@router.post("/{estimate_id}/finalize", response_model=FinalizeResponse)
def finalize_workbench(
    estimate_id: int,
    body: FinalizeRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Finalize workbench → customer Quote. Blocked while any Review/calc errors remain."""
    _ = request
    estimate = get_estimate_tree(db, estimate_id, company_id)
    if not estimate:
        raise HTTPException(status_code=404, detail="Estimate not found")
    try:
        result = finalize_estimate(
            db,
            estimate,
            company_id=company_id,
            user_id=current_user.id,
            valid_days=body.valid_days,
            force=body.force,
            audit=audit,
        )
    except FinalizeBlockedError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": exc.message,
                "blockers": exc.blockers,
                "blocker_count": len(exc.blockers),
            },
        ) from exc
    return FinalizeResponse(
        estimate_id=result["estimate_id"],
        quote_id=result["quote_id"],
        quote_number=result["quote_number"],
        grand_total=result["grand_total"],
        forced=result["forced"],
        verification=VerificationReportOut.model_validate(result["verification"]),
    )


@router.post("/{estimate_id}/extract-from-rfq", response_model=ExtractFromRfqResponse)
def extract_from_rfq(
    estimate_id: int,
    body: ExtractFromRfqRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Triple-pass (or deterministic) PDF/BOM extract → draft workbench lines.

    Default returns a staging draft. Pass ``apply=true`` + matching ``version``
    to replace (or merge) the estimate tree and store the extraction artifact.
    """
    _ = request
    estimate = get_estimate_tree(db, estimate_id, company_id)
    if not estimate:
        raise HTTPException(status_code=404, detail="Estimate not found")

    rfq_id = body.rfq_package_id or estimate.rfq_package_id
    try:
        draft = extract_workbench_draft_from_rfq(
            db,
            rfq_package_id=rfq_id,
            company_id=company_id,
            use_llm=body.use_llm,
        )
    except ExtractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    summary = ExtractionSummaryOut.model_validate(draft.get("summary") or {})
    assemblies_out = [
        AssemblyDraftOut(
            name=a.get("name") or "Assembly",
            sort_order=int(a.get("sort_order") or 0),
            assembly_labor_hrs=float(a.get("assembly_labor_hrs") or 0),
            electrical_labor_hrs=float(a.get("electrical_labor_hrs") or 0),
            fab_lines=[FabLineDraftOut.model_validate(fl) for fl in (a.get("fab_lines") or [])],
            buyout_lines=[BuyoutLineDraftOut.model_validate(bl) for bl in (a.get("buyout_lines") or [])],
        )
        for a in (draft.get("assemblies") or [])
    ]

    workbench_out = None
    applied = False
    if body.apply:
        if body.version is None:
            raise HTTPException(status_code=400, detail="version is required when apply=true")
        try:
            saved = apply_extraction_to_estimate(
                db,
                estimate,
                draft,
                expected_version=body.version,
                company_id=company_id,
                user_id=current_user.id,
                audit=audit,
                replace=body.replace,
            )
        except StaleVersionError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Estimate changed since you loaded it — refresh and merge",
                    "current_version": exc.current_version,
                },
            ) from exc
        shop = load_shop_data(db, company_id)
        workbench_out = _serialize_tree(saved, shop_data_source=shop.source)
        applied = True

    return ExtractFromRfqResponse(
        mode=draft.get("mode") or "unknown",
        assemblies=assemblies_out,
        machined_parts=draft.get("machined_parts") or [],
        summary=summary,
        warnings=list(draft.get("warnings") or []),
        applied=applied,
        workbench=workbench_out,
        extraction_artifact=draft.get("extraction_artifact"),
    )


def _package_for_estimate(db: Session, estimate, company_id: int) -> Optional[RfqPackage]:
    return (
        db.query(RfqPackage)
        .filter(RfqPackage.id == estimate.rfq_package_id, RfqPackage.company_id == company_id)
        .first()
    )


@router.get("/{estimate_id}/export/audit.xlsx")
def export_audit_xlsx(
    estimate_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Internal audit Excel — full breakdown, hours, confidence, rate snapshot."""
    _ = current_user
    estimate = get_estimate_tree(db, estimate_id, company_id)
    if not estimate:
        raise HTTPException(status_code=404, detail="Estimate not found")
    package = _package_for_estimate(db, estimate, company_id)
    data = build_workbench_audit_xlsx(estimate, package=package)
    rfq = (package.rfq_number if package else None) or f"EW-{estimate_id}"
    filename = f"{rfq}_workbench_audit.xlsx"
    return StreamingResponse(
        BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{estimate_id}/export/audit.json")
def export_audit_json(
    estimate_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Internal audit JSON — same payload as Excel, machine-readable."""
    _ = current_user
    estimate = get_estimate_tree(db, estimate_id, company_id)
    if not estimate:
        raise HTTPException(status_code=404, detail="Estimate not found")
    package = _package_for_estimate(db, estimate, company_id)
    data = build_workbench_audit_json_bytes(estimate, package=package)
    rfq = (package.rfq_number if package else None) or f"EW-{estimate_id}"
    filename = f"{rfq}_workbench_audit.json"
    return StreamingResponse(
        BytesIO(data),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{estimate_id}/export/customer.pdf")
def export_customer_pdf(
    estimate_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Customer PDF — sell totals only; blocked while Review items remain (unless finalized)."""
    _ = current_user
    estimate = get_estimate_tree(db, estimate_id, company_id)
    if not estimate:
        raise HTTPException(status_code=404, detail="Estimate not found")
    package = _package_for_estimate(db, estimate, company_id)
    try:
        pdf = build_workbench_customer_pdf(
            estimate,
            package=package,
            quote_number=None,
            require_clear_verification=True,
        )
    except ExportBlockedError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": exc.message,
                "blockers": exc.blockers,
                "blocker_count": len(exc.blockers),
            },
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    rfq = (package.rfq_number if package else None) or f"EW-{estimate_id}"
    filename = f"{rfq}_customer_quote.pdf"
    return StreamingResponse(
        BytesIO(pdf),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
