"""Tenant-scoped versioned estimates; all calculations belong to the new engine."""

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, defer

from app.fabrication_quote.engine import evaluate_plan
from app.fabrication_quote.schemas import QuotePlan
from app.fabrication_quote.schemas_api import (
    ActualObservation,
    CreateQuote,
    UpdateQuote,
)
from app.models.customer import Customer
from app.models.fabrication_quote import (
    FabricationQuote,
    FabricationQuoteActual,
    FabricationQuoteFile,
    FabricationQuoteRevision,
)
from app.models.quote import Quote, QuoteLine, QuoteStatus
from app.models.role_permission import DEFAULT_ROLE_PERMISSIONS, RolePermission
from app.models.user import User, UserRole
from app.services.audit_service import AuditService


def canonical(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def iso(value):
    return value.isoformat() + "Z" if value else None


def require_access(db: Session, user: User, company_id: int, *, write=False):
    if write and getattr(user, "_read_only_company_context", False):
        raise HTTPException(403, "This company context is read-only")
    if user.is_superuser or user.role == UserRole.PLATFORM_ADMIN:
        return
    override = (
        db.query(RolePermission)
        .filter(RolePermission.company_id == company_id, RolePermission.role == user.role)
        .first()
    )
    permissions = override.permissions if override else DEFAULT_ROLE_PERMISSIONS.get(user.role, [])
    required = {"purchasing:view", "purchasing:create"} if write else {"purchasing:view"}
    if (
        not isinstance(permissions, list)
        or not all(isinstance(p, str) for p in permissions)
        or not required.issubset(permissions)
    ):
        raise HTTPException(403, "Fabrication quoting requires " + " and ".join(sorted(required)))


def get_quote(db, company_id, quote_id):
    row = (
        db.query(FabricationQuote)
        .filter(FabricationQuote.company_id == company_id, FabricationQuote.id == quote_id)
        .first()
    )
    if row is None:
        raise HTTPException(404, "Fabrication quote not found")
    return row


def _customer(db, company_id, customer_id):
    if customer_id is None:
        return None
    row = (
        db.query(Customer)
        .filter(
            Customer.company_id == company_id,
            Customer.id == customer_id,
            Customer.is_deleted.is_(False),
        )
        .first()
    )
    if row is None:
        raise HTTPException(422, "Choose a customer in the active company")
    return row


def files_for(db, row):
    files = (
        db.query(FabricationQuoteFile)
        .options(defer(FabricationQuoteFile.content))
        .filter(
            FabricationQuoteFile.company_id == row.company_id,
            FabricationQuoteFile.quote_id == row.id,
        )
        .order_by(FabricationQuoteFile.id)
        .all()
    )
    return [
        {
            "id": f.id,
            "file_name": f.file_name,
            "sha256": f.sha256,
            "byte_count": f.byte_count,
            "units_override": f.units_override or None,
            "content_type": f.content_type,
            "analysis": f.analysis_json,
            "created_at": iso(f.created_at),
        }
        for f in files
    ]


def source_issues(plan: QuotePlan, files):
    reviews = {}
    issues = []
    for review in plan.source_reviews:
        if review.file_id in reviews:
            issues.append(
                {
                    "severity": "blocking",
                    "code": "duplicate_source_review",
                    "path": "source_reviews",
                    "message": "Each source file must have one review disposition",
                }
            )
        reviews[review.file_id] = review
    known = {f["id"] for f in files}
    for file in files:
        review = reviews.get(file["id"])
        if review is None or review.sha256 != file["sha256"] or not review.note.strip():
            issues.append(
                {
                    "severity": "blocking",
                    "code": "source_review_required",
                    "path": f"files.{file['id']}",
                    "message": f"Review {file['file_name']} and record how its requirements were included or excluded",
                }
            )
    if set(reviews) - known:
        issues.append(
            {
                "severity": "blocking",
                "code": "unknown_source_review",
                "path": "source_reviews",
                "message": "A source review references a file outside this quote",
            }
        )
    return issues


def calculate(plan, files=None, as_of=None):
    result = evaluate_plan(plan, as_of=as_of or date.today())
    if files is not None:
        issues = source_issues(plan, files) + nest_issues(plan, files, result)
        result["issues"] = result.get("issues", []) + issues
        result["can_approve"] = result.get("can_approve", False) and not issues
    return result


def nest_issues(plan, files, calculation):
    """A saved layout remains authoritative only for its exact current demand.

    Applying a saved nest is explicit: one per-batch material charge with source
    ``nest:<file_id>``. Other manual material estimates retain their stated basis.
    """
    issues = []
    by_id = {str(f["id"]): f for f in files}
    demand = {r["part_id"]: Decimal(r["quantity"]) for r in calculation.get("demand", [])}
    active_made_parts = {
        r["part_id"] for r in calculation.get("demand", []) if r["make_or_buy"] == "make" and Decimal(r["quantity"]) > 0
    }
    calculated_materials = {line["id"]: line for line in calculation.get("material_lines", [])}
    used = set()
    for index, material in enumerate(plan.materials):
        source = material.evidence.source or ""
        if not source.startswith("nest:"):
            continue

        def block(code, message):
            issues.append(
                {
                    "severity": "blocking",
                    "code": code,
                    "path": f"materials.{index}",
                    "message": message,
                }
            )

        ident = source.removeprefix("nest:")
        file = by_id.get(ident)
        if not file or file["analysis"].get("kind") != "nest":
            block(
                "missing_nest_evidence",
                "Selected material nest is not saved in this quote",
            )
            continue
        if ident in used:
            block(
                "duplicate_nest_cost",
                "Charge each selected nest's stock cost only once",
            )
        used.add(ident)
        analysis = file["analysis"]
        review = next((r for r in plan.source_reviews if str(r.file_id) == ident), None)
        if review is not None and review.disposition != "reviewed":
            block("excluded_nest", "A nest excluded from quote scope cannot supply its material cost")
        result = analysis["geometry"]
        if result.get("status") != "complete" or not result.get("validated") or not result.get("fully_priced"):
            block(
                "incomplete_nest",
                "Selected nest must place all demand and have complete stock prices",
            )
        for part in analysis["nest_input"].get("parts", []):
            if demand.get(part["id"]) != Decimal(str(part["quantity"])):
                block(
                    "stale_nest_quantity",
                    "Assembly demand changed or nest part IDs do not match the BOM; recompute the nest",
                )
                break
        prices = result.get("cost_by_currency", {})
        cost = prices.get(plan.currency)
        charged = calculated_materials.get(material.id)
        if len(prices) != 1 or cost is None:
            block("nest_currency", "Selected stock prices must use the quote currency")
        elif (
            material.quantity_basis != "per_batch"
            or material.part_id not in active_made_parts
            or material.batch_size < demand[material.part_id]
            or material.consumed_quantity is None
            or material.unit_cost is None
            or abs(material.consumed_quantity * material.unit_cost - Decimal(cost)) > Decimal("0.000001")
            or charged is None
            or charged["part_id"] != material.part_id
            or Decimal(charged["multiplier"]) != 1
            or charged["cost"] is None
            or abs(Decimal(charged["cost"]) - Decimal(cost)) > Decimal("0.000001")
        ):
            block(
                "nest_cost_allocation",
                "Allocate the selected nest's full stock cost once to an active made part, with a single per-batch material line included in calculated costs",
            )
    return issues


def serialize(db, row, *, detail=True):
    result = {
        "id": row.id,
        "title": row.title,
        "customer_id": row.customer_id,
        "status": row.status,
        "revision": row.revision,
        "calculation": row.calculation_json,
        "approved_at": iso(row.approved_at),
        "approved_by": row.approved_by,
        "approved_revision": row.approved_revision,
        "erp_quote_id": row.erp_quote_id,
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
    }
    if detail:
        result.update(plan=row.plan_json, files=files_for(db, row))
    return result


def _snapshot(db, row, action, user, note=""):
    snapshot = serialize(db, row)
    # File bytes and parser results are immutable in their own table. Preserve
    # exact identity and parser/version evidence without copying STEP meshes or
    # entire PDF extraction results into every subsequent estimate revision.
    snapshot["files"] = [
        {
            **{key: value for key, value in file.items() if key != "analysis"},
            "analysis_sha256": digest(file["analysis"]),
            "parser": file["analysis"].get("parser"),
        }
        for file in snapshot["files"]
    ]
    db.add(
        FabricationQuoteRevision(
            company_id=row.company_id,
            quote_id=row.id,
            revision=row.revision,
            action=action,
            snapshot_json=snapshot,
            content_sha256=digest(snapshot),
            note=note,
            created_by=user.id,
        )
    )
    AuditService(db, user).log_required(
        action="APPROVE" if action == "approve" else "UPDATE",
        resource_type="fabrication_quote",
        resource_id=row.id,
        resource_identifier=row.title,
        company_id=row.company_id,
        description=f"Fabrication estimate {action}",
        new_values={
            "revision": row.revision,
            "status": row.status,
            "snapshot_sha256": digest(snapshot),
        },
    )
    db.flush()


def _claim(db, row, expected_revision, allowed_statuses):
    if row.revision != expected_revision:
        raise HTTPException(409, "This estimate has changed. Reload before saving or approving.")
    if row.status not in allowed_statuses:
        raise HTTPException(409, "Create a new draft revision before changing an approved estimate")
    affected = (
        db.query(FabricationQuote)
        .filter(
            FabricationQuote.company_id == row.company_id,
            FabricationQuote.id == row.id,
            FabricationQuote.revision == expected_revision,
            FabricationQuote.status.in_(allowed_statuses),
        )
        .update(
            {
                FabricationQuote.revision: expected_revision + 1,
                FabricationQuote.updated_at: datetime.utcnow(),
            },
            synchronize_session=False,
        )
    )
    if affected != 1:
        raise HTTPException(409, "Another estimator changed this estimate. Reload before continuing.")
    db.refresh(row)


def create(db, company_id, user, value: CreateQuote):
    title = value.title.strip()
    if not title:
        raise HTTPException(422, "Quote title is required")
    _customer(db, company_id, value.customer_id)
    request_hash = digest(value.model_dump(mode="json", exclude={"request_key"}))
    if value.request_key:
        prior = (
            db.query(FabricationQuote)
            .filter(
                FabricationQuote.company_id == company_id,
                FabricationQuote.request_key == value.request_key,
            )
            .first()
        )
        if prior:
            if prior.request_hash != request_hash:
                raise HTTPException(
                    409,
                    "This creation request key was already used for different inputs",
                )
            return serialize(db, prior)
    plan = value.plan.model_dump(mode="json")
    row = FabricationQuote(
        company_id=company_id,
        title=title,
        customer_id=value.customer_id,
        status="draft",
        revision=1,
        plan_json=plan,
        calculation_json=calculate(value.plan, []),
        request_key=value.request_key,
        request_hash=request_hash,
        created_by=user.id,
    )
    db.add(row)
    db.flush()
    _snapshot(db, row, "create", user)
    return serialize(db, row)


def update(db, company_id, user, quote_id, value: UpdateQuote):
    row = get_quote(db, company_id, quote_id)
    _customer(db, company_id, value.customer_id)
    if not value.title.strip():
        raise HTTPException(422, "Quote title is required")
    _claim(db, row, value.expected_revision, ["draft"])
    row.title = value.title.strip()
    row.customer_id = value.customer_id
    row.plan_json = value.plan.model_dump(mode="json")
    row.calculation_json = calculate(value.plan, files_for(db, row))
    _snapshot(db, row, "save", user)
    return serialize(db, row)


def approve(db, company_id, user, quote_id, expected_revision, note):
    row = get_quote(db, company_id, quote_id)
    if not note.strip():
        raise HTTPException(422, "Record the estimator's review before approval")
    plan = QuotePlan.model_validate(row.plan_json)
    calculation = calculate(plan, files_for(db, row))
    if not calculation["can_approve"]:
        raise HTTPException(
            422,
            {
                "message": "Resolve the estimate's blocking issues before approval",
                "issues": calculation["issues"],
            },
        )
    _claim(db, row, expected_revision, ["draft"])
    row.calculation_json = calculation
    row.status = "approved"
    row.approved_revision = row.revision
    row.approved_by = user.id
    row.approved_at = datetime.utcnow()
    _snapshot(db, row, "approve", user, note.strip())
    return serialize(db, row)


def revise(db, company_id, user, quote_id, expected_revision, note):
    row = get_quote(db, company_id, quote_id)
    _claim(db, row, expected_revision, ["approved", "handed_off"])
    row.status = "draft"
    row.approved_revision = None
    row.approved_at = None
    row.approved_by = None
    row.erp_quote_id = None
    row.calculation_json = calculate(QuotePlan.model_validate(row.plan_json), files_for(db, row))
    _snapshot(db, row, "revise", user, note)
    return serialize(db, row)


def attach_file(
    db,
    company_id,
    user,
    quote_id,
    expected_revision,
    content,
    name,
    units_override,
    analysis,
):
    row = get_quote(db, company_id, quote_id)
    sha = hashlib.sha256(content).hexdigest()
    if row.revision != expected_revision or row.status != "draft":
        raise HTTPException(409, "Reload the draft before attaching this file")
    existing = (
        db.query(FabricationQuoteFile)
        .filter(
            FabricationQuoteFile.company_id == company_id,
            FabricationQuoteFile.quote_id == quote_id,
            FabricationQuoteFile.sha256 == sha,
            FabricationQuoteFile.units_override == (units_override or ""),
        )
        .first()
    )
    if existing:
        return serialize(db, row)
    count, total = (
        db.query(
            func.count(FabricationQuoteFile.id),
            func.sum(FabricationQuoteFile.byte_count),
        )
        .filter(
            FabricationQuoteFile.company_id == company_id,
            FabricationQuoteFile.quote_id == quote_id,
        )
        .one()
    )
    if count >= 100 or (total or 0) + len(content) > 100 * 1024 * 1024:
        raise HTTPException(413, "A quote can hold up to 100 source files totaling 100 MiB")
    _claim(db, row, expected_revision, ["draft"])
    media = "application/pdf" if name.lower().endswith(".pdf") else "application/octet-stream"
    db.add(
        FabricationQuoteFile(
            company_id=company_id,
            quote_id=quote_id,
            file_name=name,
            sha256=sha,
            byte_count=len(content),
            units_override=units_override or "",
            content_type=media,
            content=content,
            analysis_json=analysis,
            created_by=user.id,
        )
    )
    db.flush()
    row.calculation_json = calculate(QuotePlan.model_validate(row.plan_json), files_for(db, row))
    _snapshot(db, row, "attach_file", user)
    return serialize(db, row)


def handoff(db, company_id, user, quote_id, expected_revision):
    row = get_quote(db, company_id, quote_id)
    # An identical retry may carry the revision before the successful handoff.
    if row.status == "handed_off" and row.erp_quote_id and expected_revision in (row.revision, row.revision - 1):
        return serialize(db, row)
    if not row.customer_id:
        raise HTTPException(422, "Choose an ERP customer before creating the ERP quote")
    customer = _customer(db, company_id, row.customer_id)
    if row.status != "approved":
        raise HTTPException(409, "Approve the estimate before creating the ERP quote")
    # Validate freshness at handoff, but never silently replace an approved cost.
    fresh = calculate(QuotePlan.model_validate(row.plan_json), files_for(db, row))
    if not fresh["can_approve"] or fresh["totals"] != row.calculation_json["totals"]:
        raise HTTPException(409, "The approved pricing needs a new review before handoff")
    if row.plan_json.get("currency") != "USD":
        raise HTTPException(
            422,
            "ERP quote records do not store currency; handoff currently requires a USD estimate",
        )
    approved_revision = row.approved_revision
    _claim(db, row, expected_revision, ["approved"])
    totals = row.calculation_json["totals"]
    price = totals.get("selling_price")
    if price is None:
        raise HTTPException(422, "Set and review the selling margin before handoff")
    # The ERP's current order conversion cannot represent an arbitrary assembly
    # graph. Preserve the approved package as one priced line; the complete
    # manufacturing manifest is attached through the source revision identity.
    quote = Quote(
        company_id=company_id,
        quote_number=f"FAB-{company_id}-{row.id}-R{approved_revision}",
        revision=str(approved_revision),
        request_key=f"fabrication-{row.id}-{approved_revision}",
        request_hash=digest(row.calculation_json),
        customer_name=customer.name,
        customer_contact=customer.contact_name,
        customer_email=customer.email,
        customer_phone=customer.phone,
        payment_terms=customer.payment_terms,
        status=QuoteStatus.DRAFT,
        subtotal=float(price),
        tax=0,
        total=float(price),
        created_by=user.id,
        approved_by=row.approved_by,
        approved_at=row.approved_at,
        internal_notes=f"Source fabrication estimate {row.id}, approved revision {approved_revision}. Review the manufacturing package before creating released BOMs or work orders.",
    )
    db.add(quote)
    db.flush()

    # The older ERP line has only three cost buckets. Map every cost category
    # exactly once; procurement cash is not an additional manufacturing cost.
    # Authoritative Decimal values remain in the immutable source snapshot.
    def cost_sum(*keys):
        return sum((Decimal(totals[key]) for key in keys), Decimal("0"))

    material_cost = cost_sum("material_cost", "purchased_parts_cost", "hardware_consumed_cost")
    overhead_cost = cost_sum("machine_cost", "consumables_cost", "outside_cost")
    labor_hours = sum(
        (Decimal(op["labor_seconds"]) for op in row.calculation_json["operation_lines"]),
        Decimal("0"),
    ) / Decimal("3600")
    margin = Decimal(totals["target_margin"])
    db.add(
        QuoteLine(
            company_id=company_id,
            quote_id=quote.id,
            line_number=1,
            description=row.title + " — complete quoted package",
            quantity=1,
            unit_price=float(price),
            line_total=float(price),
            material_cost=float(material_cost),
            labor_hours=float(labor_hours),
            labor_cost=float(totals["labor_cost"]),
            overhead_cost=float(overhead_cost),
            markup_pct=float(margin / (Decimal("1") - margin) * Decimal("100")),
            notes=f"Manufacturing plan: fabrication estimate {row.id}, revision {approved_revision}. Material includes bought assemblies and consumed hardware; overhead includes machine, consumables, and outside processes.",
        )
    )
    row.erp_quote_id = quote.id
    row.status = "handed_off"
    _snapshot(db, row, "handoff", user)
    return serialize(db, row)


def record_actual(db, company_id, user, quote_id, value: ActualObservation):
    get_quote(db, company_id, quote_id)
    rev = (
        db.query(FabricationQuoteRevision)
        .filter(
            FabricationQuoteRevision.company_id == company_id,
            FabricationQuoteRevision.quote_id == quote_id,
            FabricationQuoteRevision.revision == value.quote_revision,
        )
        .first()
    )
    if not rev or rev.action != "approve":
        raise HTTPException(422, "Link actuals to an approved estimate revision")
    # Unreachable definitions and work below a make/buy boundary can remain in
    # the editable plan. They were not part of this approved costed routing.
    operations = {o["id"] for o in rev.snapshot_json["calculation"]["operation_lines"]}
    if value.operation_id not in operations:
        raise HTTPException(422, "Operation is not in the approved manufacturing plan")
    if value.observed_on > date.today():
        raise HTTPException(422, "Actual production cannot be dated in the future")
    if not value.source.strip() or not value.note.strip():
        raise HTTPException(422, "Actual observations require a meaningful source and note")
    if value.completeness == "complete" and any(
        value.model_dump()[key] is None for key in ("setup_labor_seconds", "run_labor_seconds", "machine_seconds")
    ):
        raise HTTPException(
            422,
            "Complete time observations require setup labor, run labor and machine seconds; enter explicit zero when none applies",
        )
    observation = value.model_dump(mode="json", exclude={"request_key", "quote_revision"})
    existing = (
        db.query(FabricationQuoteActual)
        .filter(
            FabricationQuoteActual.company_id == company_id,
            FabricationQuoteActual.request_key == value.request_key,
        )
        .first()
    )
    if existing:
        if (
            existing.quote_id != quote_id
            or existing.quote_revision != value.quote_revision
            or existing.observation_json != observation
        ):
            raise HTTPException(409, "This actuals request key was used for different observations")
        return {
            "id": existing.id,
            "quote_revision": existing.quote_revision,
            **existing.observation_json,
        }
    row = FabricationQuoteActual(
        company_id=company_id,
        quote_id=quote_id,
        quote_revision=value.quote_revision,
        request_key=value.request_key,
        observation_json=observation,
        created_by=user.id,
    )
    db.add(row)
    db.flush()
    AuditService(db, user).log_required(
        "CREATE",
        "fabrication_quote_actual",
        resource_id=row.id,
        company_id=company_id,
        new_values={
            "quote_id": quote_id,
            "revision": value.quote_revision,
            "operation_id": value.operation_id,
        },
    )
    return {"id": row.id, "quote_revision": row.quote_revision, **observation}
