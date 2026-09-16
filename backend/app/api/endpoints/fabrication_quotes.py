"""Replacement estimator API; identity and order handoff remain ERP adapters."""

import hashlib
import json
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.api.deps import get_current_company_id, get_current_user
from app.db.database import atomic_transaction, get_db
from app.fabrication_quote.schemas_api import (
    ActualObservation,
    CalculateQuote,
    CreateQuote,
    RevisionAction,
    UpdateQuote,
)
from app.fabrication_quote.worker import analyze_in_worker, nest_in_worker
from app.models.fabrication_quote import (
    FabricationQuote,
    FabricationQuoteActual,
    FabricationQuoteFile,
    FabricationQuoteRevision,
)
from app.models.user import User
from app.services import fabrication_quote_service as service
from app.services.audit_service import AuditWriteError

router = APIRouter()


def read_user(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    service.require_access(db, user, company_id)
    return user


def write_user(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    service.require_access(db, user, company_id, write=True)
    return user


def mutate(db, function, *args):
    try:
        with atomic_transaction(db):
            return function(db, *args)
    except AuditWriteError as exc:
        raise HTTPException(503, "The change was not saved because its audit record could not be recorded") from exc
    except IntegrityError as exc:
        raise HTTPException(409, "A concurrent change conflicts with this request. Reload and retry.") from exc


@router.get("")
def list_quotes(
    search: str = Query("", max_length=200),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    query = db.query(FabricationQuote).filter(FabricationQuote.company_id == company_id)
    if search:
        query = query.filter(
            FabricationQuote.title.ilike("%" + search.replace("%", "\\%").replace("_", "\\_") + "%", escape="\\")
        )
    total = query.count()
    rows = (
        query.order_by(FabricationQuote.updated_at.desc(), FabricationQuote.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return {"items": [service.serialize(db, row, detail=False) for row in rows], "total": total}


@router.post("/calculate")
def calculate(
    value: CalculateQuote,
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    files = []
    if value.quote_id is not None:
        files = service.files_for(db, service.get_quote(db, company_id, value.quote_id))
    return service.calculate(value.plan, files, as_of=value.as_of)


@router.get("/capabilities")
def capabilities(
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    can_write = True
    try:
        service.require_access(db, user, company_id, write=True)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        can_write = False
    return {
        "can_write": can_write,
        "machine_connection_required": False,
        "max_source_bytes": 25 * 1024 * 1024,
        "max_json_bytes": 2 * 1024 * 1024,
    }


@router.post("/nest")
async def nest(request: Request, user: User = Depends(read_user)):
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("Nesting input must be an object")
        result = await run_in_threadpool(nest_in_worker, payload)
        if result.get("status") == "error":
            raise ValueError(result.get("message", "Nesting input is invalid"))
        return result
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(422, str(exc)[:300]) from exc


@router.post("")
def create_quote(
    value: CreateQuote,
    db: Session = Depends(get_db),
    user: User = Depends(write_user),
    company_id: int = Depends(get_current_company_id),
):
    return mutate(db, service.create, company_id, user, value)


@router.get("/{quote_id}")
def get_quote(
    quote_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    return service.serialize(db, service.get_quote(db, company_id, quote_id))


@router.put("/{quote_id}")
def update_quote(
    quote_id: int,
    value: UpdateQuote,
    db: Session = Depends(get_db),
    user: User = Depends(write_user),
    company_id: int = Depends(get_current_company_id),
):
    return mutate(db, service.update, company_id, user, quote_id, value)


@router.post("/{quote_id}/approve")
def approve_quote(
    quote_id: int,
    value: RevisionAction,
    db: Session = Depends(get_db),
    user: User = Depends(write_user),
    company_id: int = Depends(get_current_company_id),
):
    return mutate(db, service.approve, company_id, user, quote_id, value.expected_revision, value.review_note)


@router.post("/{quote_id}/revise")
def revise_quote(
    quote_id: int,
    value: RevisionAction,
    db: Session = Depends(get_db),
    user: User = Depends(write_user),
    company_id: int = Depends(get_current_company_id),
):
    return mutate(db, service.revise, company_id, user, quote_id, value.expected_revision, value.review_note)


@router.post("/{quote_id}/handoff")
def handoff_quote(
    quote_id: int,
    value: RevisionAction,
    db: Session = Depends(get_db),
    user: User = Depends(write_user),
    company_id: int = Depends(get_current_company_id),
):
    return mutate(db, service.handoff, company_id, user, quote_id, value.expected_revision)


@router.post("/{quote_id}/files")
async def upload_file(
    quote_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(write_user),
    company_id: int = Depends(get_current_company_id),
):
    service.get_quote(db, company_id, quote_id)
    async with request.form(max_files=1, max_fields=2, max_part_size=4096) as form:
        if set(form) - {"file", "expected_revision", "units_override"} or len(form.multi_items()) != len(form):
            raise HTTPException(422, "Provide one file, expected_revision, and optional units_override")
        upload = form.get("file")
        expected = form.get("expected_revision")
        units = form.get("units_override") or None
        if (
            not isinstance(upload, UploadFile)
            or not isinstance(expected, str)
            or not expected.isascii()
            or not expected.isdigit()
            or len(expected) > 9
        ):
            raise HTTPException(422, "A source file and current expected_revision are required")
        if units not in (None, "mm", "inch", "in", "cm", "m"):
            raise HTTPException(422, "Units override must be mm or inch")
        if units == "inch":
            units = "in"
        name = PurePosixPath((upload.filename or "source").replace("\\", "/")).name
        if not name or len(name) > 255 or any(ord(c) < 32 for c in name):
            raise HTTPException(422, "Source filename is invalid")
        if PurePosixPath(name.lower()).suffix not in (".dxf", ".step", ".stp", ".pdf", ".csv"):
            raise HTTPException(422, "Supported sources: DXF, STEP/STP, PDF, and BOM/offer CSV")
        content = await upload.read(25 * 1024 * 1024 + 1)
        if not content or len(content) > 25 * 1024 * 1024:
            raise HTTPException(413, "Upload a nonempty source file up to 25 MiB")
    # Check concurrency before spending parser time, and again when writing.
    current = service.get_quote(db, company_id, quote_id)
    if current.revision != int(expected) or current.status != "draft":
        raise HTTPException(409, "Reload the draft before attaching a file")
    try:
        analysis = await run_in_threadpool(analyze_in_worker, content, name, units)
    except ValueError as exc:
        analysis = {
            "file_name": name,
            "sha256": hashlib.sha256(content).hexdigest(),
            "kind": PurePosixPath(name).suffix.lstrip("."),
            "status": "error",
            "parser": "isolated-worker",
            "units": None,
            "observations": [],
            "geometry": None,
            "pages": [],
            "issues": [{"severity": "blocking", "code": "analysis_failed", "message": str(exc)}],
        }
    return await run_in_threadpool(
        mutate, db, service.attach_file, company_id, user, quote_id, int(expected), content, name, units, analysis
    )


@router.post("/{quote_id}/nests")
async def save_nest(
    quote_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(write_user),
    company_id: int = Depends(get_current_company_id),
):
    current = service.get_quote(db, company_id, quote_id)
    payload = await request.json()
    if (
        not isinstance(payload, dict)
        or set(payload) != {"expected_revision", "input"}
        or not isinstance(payload["input"], dict)
    ):
        raise HTTPException(422, "Provide expected_revision and the nesting input")
    expected = payload["expected_revision"]
    if (
        isinstance(expected, bool)
        or not isinstance(expected, int)
        or expected != current.revision
        or current.status != "draft"
    ):
        raise HTTPException(409, "Reload the draft before saving a nest")
    try:
        result = await run_in_threadpool(nest_in_worker, payload["input"])
        if result.get("status") == "error":
            raise ValueError(result.get("message", "Nesting input is invalid"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    content = service.canonical(payload["input"]).encode()
    analysis = {
        "kind": "nest",
        "status": result["status"],
        "parser": result["solver"],
        "units": "mm",
        "geometry": result,
        "nest_input": payload["input"],
        "observations": [],
        "pages": [],
        "issues": result.get("issues", []),
    }
    name = "estimating-nest-" + hashlib.sha256(content).hexdigest()[:12] + ".json"
    return await run_in_threadpool(
        mutate, db, service.attach_file, company_id, user, quote_id, expected, content, name, None, analysis
    )


@router.get("/{quote_id}/files/{file_id}/content")
def original_file(
    quote_id: int,
    file_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    file = (
        db.query(FabricationQuoteFile)
        .filter(
            FabricationQuoteFile.company_id == company_id,
            FabricationQuoteFile.quote_id == quote_id,
            FabricationQuoteFile.id == file_id,
        )
        .first()
    )
    if not file:
        raise HTTPException(404, "Source file not found")
    return Response(
        content=file.content,
        media_type=file.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="source-{file.id}{PurePosixPath(file.file_name).suffix.lower()}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/{quote_id}/revisions")
def revisions(
    quote_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    service.get_quote(db, company_id, quote_id)
    rows = (
        db.query(FabricationQuoteRevision)
        .filter(FabricationQuoteRevision.company_id == company_id, FabricationQuoteRevision.quote_id == quote_id)
        .order_by(FabricationQuoteRevision.revision.desc())
        .limit(200)
        .all()
    )
    return {
        "items": [
            {
                "revision": r.revision,
                "action": r.action,
                "note": r.note,
                "created_at": service.iso(r.created_at),
                "content_sha256": r.content_sha256,
            }
            for r in rows
        ]
    }


@router.get("/{quote_id}/revisions/{number}")
def revision(
    quote_id: int,
    number: int,
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    row = (
        db.query(FabricationQuoteRevision)
        .filter(
            FabricationQuoteRevision.company_id == company_id,
            FabricationQuoteRevision.quote_id == quote_id,
            FabricationQuoteRevision.revision == number,
        )
        .first()
    )
    if not row:
        raise HTTPException(404, "Quote revision not found")
    return row.snapshot_json


@router.get("/{quote_id}/export")
def export_package(
    quote_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    quote = service.get_quote(db, company_id, quote_id)
    if not quote.approved_revision:
        raise HTTPException(409, "Approve the manufacturing package before exporting")
    row = (
        db.query(FabricationQuoteRevision)
        .filter(
            FabricationQuoteRevision.company_id == company_id,
            FabricationQuoteRevision.quote_id == quote_id,
            FabricationQuoteRevision.revision == quote.approved_revision,
        )
        .one()
    )
    return Response(
        content=json.dumps(
            {
                "format": "werco-fabrication-package-v1",
                "snapshot_sha256": row.content_sha256,
                "snapshot": row.snapshot_json,
            },
            indent=2,
            ensure_ascii=False,
        ),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="fabrication-{quote_id}-r{row.revision}.json"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/{quote_id}/actuals")
def list_actuals(
    quote_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    service.get_quote(db, company_id, quote_id)
    rows = (
        db.query(FabricationQuoteActual)
        .filter(FabricationQuoteActual.company_id == company_id, FabricationQuoteActual.quote_id == quote_id)
        .order_by(FabricationQuoteActual.created_at.desc())
        .limit(500)
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "quote_revision": r.quote_revision,
                "created_at": service.iso(r.created_at),
                **r.observation_json,
            }
            for r in rows
        ]
    }


@router.post("/{quote_id}/actuals")
def record_actuals(
    quote_id: int,
    value: ActualObservation,
    db: Session = Depends(get_db),
    user: User = Depends(write_user),
    company_id: int = Depends(get_current_company_id),
):
    return mutate(db, service.record_actual, company_id, user, quote_id, value)
