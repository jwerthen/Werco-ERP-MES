from __future__ import annotations

from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_user, require_role
from app.core.logging import get_logger
from app.db.database import get_db
from app.models.customer import Customer
from app.models.quote import Quote, QuoteLine, QuoteStatus
from app.models.quote_config import QuoteFinish, QuoteSettings
from app.models.rfq_quote import (
    QuoteEstimate,
    QuoteLineSummary,
    RfqPackage,
    RfqPackageFile,
)
from app.models.user import User, UserRole
from app.services.rfq_parsing_service import parse_rfq_package_files
from app.services.rfq_pricing_service import MaterialPriceService
from app.services.sheet_metal_costing_service import (
    SheetMetalCostConfig,
    calc_bending_cost,
    calc_cutting_cost,
    calc_finishing_cost,
    calc_margin,
    calc_material_cost,
    calc_required_weight_lbs,
    calc_shop_labor_oh,
    calc_weld_assembly_cost,
    estimate_lead_time_range,
    normalize_material,
)

router = APIRouter()
logger = get_logger(__name__)

UPLOAD_DIR = Path("uploads/rfq_packages")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_EXTENSIONS = {".pdf", ".xlsx", ".xls", ".dxf", ".step", ".stp"}


class RfqPackageResponse(BaseModel):
    id: int
    rfq_number: str
    customer_id: Optional[int] = None
    customer_name: Optional[str] = None
    rfq_reference: Optional[str] = None
    status: str
    warnings: List[str] = []
    file_count: int
    quote_id: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True


class GenerateEstimateRequest(BaseModel):
    target_margin_pct: Optional[float] = None
    valid_days: int = 30


class QuoteLineSummaryResponse(BaseModel):
    part_number: Optional[str] = None
    part_name: str
    quantity: float
    material: Optional[str] = None
    thickness: Optional[str] = None
    flat_area: Optional[float] = None
    cut_length: Optional[float] = None
    hole_count: Optional[int] = None
    bend_count: Optional[int] = None
    finish: Optional[str] = None
    part_total: float
    confidence: Dict[str, float] = {}
    sources: Dict[str, List[str]] = {}


class QuoteEstimateResponse(BaseModel):
    rfq_package_id: int
    estimate_id: int
    quote_id: int
    quote_number: str
    totals: Dict[str, float]
    lead_time: Dict[str, Any]
    confidence: Dict[str, Any]
    assumptions: List[Dict[str, Any]]
    missing_specs: List[Dict[str, Any]]
    source_attribution: Dict[str, List[str]]
    line_summaries: List[QuoteLineSummaryResponse]


def _get_setting_number(db: Session, key: str, default: float) -> float:
    setting = db.query(QuoteSettings).filter(QuoteSettings.setting_key == key).first()
    if not setting:
        return default
    try:
        return float(setting.setting_value)
    except Exception:
        return default


def _generate_rfq_number(db: Session) -> str:
    today = datetime.utcnow().strftime("%Y%m%d")
    prefix = f"RFQ-{today}-"
    last = (
        db.query(RfqPackage)
        .filter(RfqPackage.rfq_number.like(f"{prefix}%"))
        .order_by(RfqPackage.rfq_number.desc())
        .first()
    )
    next_num = 1
    if last:
        try:
            next_num = int(last.rfq_number.split("-")[-1]) + 1
        except Exception:
            next_num = 1
    return f"{prefix}{next_num:03d}"


def _generate_quote_number(db: Session) -> str:
    today = datetime.utcnow().strftime("%Y%m")
    prefix = f"QTE-{today}-"
    last = (
        db.query(Quote)
        .filter(Quote.quote_number.like(f"{prefix}%"))
        .order_by(Quote.quote_number.desc())
        .first()
    )
    next_num = 1
    if last:
        try:
            next_num = int(last.quote_number.split("-")[-1]) + 1
        except Exception:
            next_num = 1
    return f"{prefix}{next_num:04d}"


def _resolve_customer_name(db: Session, customer_id: Optional[int], customer_name: Optional[str]) -> Optional[str]:
    if customer_name:
        return customer_name.strip()
    if not customer_id:
        return None
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    return customer.name if customer else None


def _thickness_to_str(part_spec: Dict[str, Any]) -> Optional[str]:
    thickness = part_spec.get("thickness")
    if thickness:
        return str(thickness)
    thickness_in = part_spec.get("thickness_in")
    if thickness_in:
        return f"{float(thickness_in):.4f}"
    return None


def _thickness_to_float(part_spec: Dict[str, Any]) -> Optional[float]:
    value = part_spec.get("thickness_in")
    if value is not None:
        try:
            return float(value)
        except Exception:
            pass
    thickness_str = str(part_spec.get("thickness") or "").strip().lower()
    if not thickness_str:
        return None
    if thickness_str.endswith("ga"):
        gauge_map = {
            "24ga": 0.0239,
            "22ga": 0.0299,
            "20ga": 0.0359,
            "18ga": 0.0478,
            "16ga": 0.0598,
            "14ga": 0.0747,
            "12ga": 0.1046,
            "11ga": 0.1196,
            "10ga": 0.1345,
            "7ga": 0.1793,
        }
        return gauge_map.get(thickness_str)
    try:
        return float(thickness_str.replace("in", "").replace('"', "").strip())
    except Exception:
        return None


@router.post("/", response_model=RfqPackageResponse)
async def create_rfq_package(
    customer_id: Optional[int] = Form(None),
    customer_name: Optional[str] = Form(None),
    rfq_reference: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
):
    if not files:
        raise HTTPException(status_code=400, detail="At least one RFQ file is required.")

    rfq_number = _generate_rfq_number(db)
    package_dir = UPLOAD_DIR / rfq_number
    package_dir.mkdir(parents=True, exist_ok=True)

    package = RfqPackage(
        rfq_number=rfq_number,
        customer_id=customer_id,
        customer_name=_resolve_customer_name(db, customer_id, customer_name),
        rfq_reference=rfq_reference,
        status="uploaded",
        package_metadata={"notes": notes or "", "uploaded_file_count": len(files)},
        uploaded_by=current_user.id,
    )
    db.add(package)
    db.flush()

    file_count = 0
    for upload in files:
        ext = Path(upload.filename or "").suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"Unsupported file type: {upload.filename}")
        content = await upload.read()
        unique_name = f"{uuid.uuid4().hex}{ext}"
        file_path = package_dir / unique_name
        with open(file_path, "wb") as handle:
            handle.write(content)

        db.add(
            RfqPackageFile(
                rfq_package_id=package.id,
                file_name=upload.filename or unique_name,
                file_path=str(file_path),
                file_ext=ext,
                mime_type=upload.content_type,
                file_size=len(content),
                parse_status="pending",
            )
        )
        file_count += 1

    db.commit()
    db.refresh(package)

    return RfqPackageResponse(
        id=package.id,
        rfq_number=package.rfq_number,
        customer_id=package.customer_id,
        customer_name=package.customer_name,
        rfq_reference=package.rfq_reference,
        status=package.status,
        warnings=[],
        file_count=file_count,
        created_at=package.created_at,
    )


@router.get("/{package_id}", response_model=RfqPackageResponse)
def get_rfq_package(
    package_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    package = (
        db.query(RfqPackage)
        .options(joinedload(RfqPackage.files), joinedload(RfqPackage.estimates))
        .filter(RfqPackage.id == package_id)
        .first()
    )
    if not package:
        raise HTTPException(status_code=404, detail="RFQ package not found")
    latest_estimate = max(package.estimates, key=lambda item: item.created_at, default=None)
    return RfqPackageResponse(
        id=package.id,
        rfq_number=package.rfq_number,
        customer_id=package.customer_id,
        customer_name=package.customer_name,
        rfq_reference=package.rfq_reference,
        status=package.status,
        warnings=package.parsing_warnings or [],
        file_count=len(package.files),
        quote_id=latest_estimate.quote_id if latest_estimate else None,
        created_at=package.created_at,
    )


@router.post("/{package_id}/generate-estimate", response_model=QuoteEstimateResponse)
def generate_estimate(
    package_id: int,
    request: GenerateEstimateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
):
    package = (
        db.query(RfqPackage)
        .options(joinedload(RfqPackage.files))
        .filter(RfqPackage.id == package_id)
        .first()
    )
    if not package:
        raise HTTPException(status_code=404, detail="RFQ package not found")
    if not package.files:
        raise HTTPException(status_code=400, detail="RFQ package has no files.")

    parsed = parse_rfq_package_files(package.files)
    for file_record in package.files:
        result = parsed["file_results"].get(file_record.id, {})
        file_record.parse_status = result.get("parse_status", "pending")
        file_record.parse_error = result.get("parse_error")
        file_record.extracted_summary = result.get("summary")

    if not parsed["parts"]:
        raise HTTPException(status_code=400, detail="No parsable part data found in RFQ files.")

    customer_name = package.customer_name or "Unspecified Customer"
    quote = Quote(
        quote_number=_generate_quote_number(db),
        customer_name=customer_name,
        customer_contact=None,
        customer_email=None,
        status=QuoteStatus.DRAFT,
        quote_date=date.today(),
        valid_until=date.today() + timedelta(days=max(request.valid_days, 1)),
        notes=f"Generated from RFQ package {package.rfq_number}",
        created_by=current_user.id,
    )
    db.add(quote)
    db.flush()

    estimate = QuoteEstimate(
        rfq_package_id=package.id,
        quote_id=quote.id,
        created_by=current_user.id,
        assumptions=[],
        missing_specs=[],
        source_attribution={},
        confidence_detail={},
        internal_breakdown={},
    )
    db.add(estimate)
    db.flush()

    target_margin_pct = (
        request.target_margin_pct
        if request.target_margin_pct is not None
        else _get_setting_number(db, "default_markup_pct", 22.0)
    )
    config = SheetMetalCostConfig(
        scrap_factor=_get_setting_number(db, "rfq_scrap_factor", 0.10),
        laser_rate_per_hour=_get_setting_number(db, "rfq_laser_rate_per_hour", 150.0),
        brake_rate_per_hour=_get_setting_number(db, "rfq_brake_rate_per_hour", 85.0),
        welding_rate_per_hour=_get_setting_number(db, "rfq_welding_rate_per_hour", 95.0),
        assembly_rate_per_hour=_get_setting_number(db, "rfq_assembly_rate_per_hour", 70.0),
        shop_overhead_pct=_get_setting_number(db, "rfq_shop_overhead_pct", 20.0),
        sec_per_bend=_get_setting_number(db, "rfq_sec_per_bend", 30.0),
        bend_setup_minutes=_get_setting_number(db, "rfq_bend_setup_minutes", 8.0),
        laser_setup_minutes=_get_setting_number(db, "rfq_laser_setup_minutes", 12.0),
        weld_minutes_per_part=_get_setting_number(db, "rfq_weld_minutes_per_part", 12.0),
        assembly_minutes_per_part=_get_setting_number(db, "rfq_assembly_minutes_per_part", 10.0),
        finish_default_rate_per_sqft=_get_setting_number(db, "rfq_finish_rate_per_sqft", 8.0),
        base_queue_days=int(_get_setting_number(db, "rfq_base_queue_days", 3)),
        effective_daily_capacity_hours=_get_setting_number(db, "rfq_daily_capacity_hours", 24.0),
        outside_service_buffer_days=int(_get_setting_number(db, "rfq_outside_service_buffer_days", 0)),
        target_margin_pct=target_margin_pct,
    )

    pricing = MaterialPriceService()
    material_total = 0.0
    outside_total = 0.0
    shop_labor_oh_total = 0.0
    total_shop_hours = 0.0
    max_outside_days = 0

    line_payloads: List[Dict[str, Any]] = []
    confidence_values: List[float] = []
    assumptions = list(parsed["assumptions"])
    missing_specs = list(parsed["missing_specs"])

    for idx, part in enumerate(parsed["parts"], start=1):
        qty = float(part.get("qty") or 1.0)
        material_name = part.get("material") or "Carbon Steel"
        material_key = normalize_material(material_name) or "carbon_steel"
        thickness_str = _thickness_to_str(part)
        thickness_in = _thickness_to_float(part) or 0.0
        flat_area = float(part.get("flat_area") or 0.0)
        cut_length = float(part.get("cut_length") or 0.0)
        bend_count = int(part.get("bend_count") or 0)
        hole_count = int(part.get("hole_count") or 0) if part.get("hole_count") is not None else None
        finish_name = part.get("finish")

        material_price = pricing.get_material_price(
            db=db,
            material=material_name,
            thickness=thickness_str,
            rfq_package_id=package.id,
            quote_estimate_id=estimate.id,
        )
        if material_price.is_fallback:
            assumptions.append(
                {
                    "part_id": part.get("part_id"),
                    "field": "material_price",
                    "assumption": material_price.notes or "Fallback material price used.",
                    "confidence": 0.45,
                }
            )

        required_weight = calc_required_weight_lbs(
            flat_area_in2=flat_area,
            thickness_in=thickness_in,
            material_key=material_key,
            quantity=qty,
        )
        part_material_cost = calc_material_cost(
            required_weight_lbs=required_weight,
            unit_price_per_lb=material_price.unit_price,
            scrap_factor=config.scrap_factor,
        )

        cutting = calc_cutting_cost(
            cut_length_in=cut_length,
            quantity=qty,
            material_key=material_key,
            machine_rate_per_hour=config.laser_rate_per_hour,
            setup_minutes=config.laser_setup_minutes,
        )
        bending = calc_bending_cost(
            bend_count=bend_count,
            quantity=qty,
            sec_per_bend=config.sec_per_bend,
            setup_minutes=config.bend_setup_minutes,
            brake_rate_per_hour=config.brake_rate_per_hour,
        )
        weld_assembly = calc_weld_assembly_cost(
            weld_required=bool(part.get("weld_required")),
            assembly_required=bool(part.get("assembly_required")),
            quantity=qty,
            weld_minutes_per_part=config.weld_minutes_per_part,
            assembly_minutes_per_part=config.assembly_minutes_per_part,
            welding_rate_per_hour=config.welding_rate_per_hour,
            assembly_rate_per_hour=config.assembly_rate_per_hour,
        )
        direct_labor = cutting["cost"] + bending["cost"] + weld_assembly["cost"]
        part_shop_labor_oh = calc_shop_labor_oh(direct_labor, config.shop_overhead_pct)

        finish_rate = None
        finish_days = 0
        if finish_name:
            finish = (
                db.query(QuoteFinish)
                .filter(QuoteFinish.is_active.is_(True), QuoteFinish.name.ilike(f"%{finish_name}%"))
                .first()
            )
            if finish and finish.price_per_sqft and finish.price_per_sqft > 0:
                finish_rate = float(finish.price_per_sqft)
            if finish and finish.additional_days:
                finish_days = int(finish.additional_days)
        part_outside_cost = calc_finishing_cost(
            finish=finish_name,
            flat_area_in2=flat_area,
            quantity=qty,
            finish_rate_per_sqft=finish_rate or config.finish_default_rate_per_sqft,
        )
        if finish_name and not finish_rate:
            assumptions.append(
                {
                    "part_id": part.get("part_id"),
                    "field": "finish_cost",
                    "assumption": "Finish table missing exact match; using default $/sqft finish model.",
                    "confidence": 0.55,
                }
            )

        if finish_name:
            max_outside_days = max(max_outside_days, finish_days or config.finish_default_outside_service_days)

        part_subtotal_no_margin = part_material_cost + part_shop_labor_oh + part_outside_cost

        material_total += part_material_cost
        shop_labor_oh_total += part_shop_labor_oh
        outside_total += part_outside_cost
        total_shop_hours += cutting["hours"] + bending["hours"] + weld_assembly["hours"]

        line_payloads.append(
            {
                "line_number": idx,
                "part_number": part.get("part_id"),
                "part_name": part.get("part_name") or str(part.get("part_id")),
                "quantity": qty,
                "material": material_name,
                "thickness": thickness_str,
                "flat_area": flat_area if flat_area > 0 else None,
                "cut_length": cut_length if cut_length > 0 else None,
                "hole_count": hole_count,
                "bend_count": bend_count,
                "finish": finish_name,
                "weld_required": bool(part.get("weld_required")),
                "assembly_required": bool(part.get("assembly_required")),
                "confidence": part.get("confidence") or {},
                "sources": part.get("sources") or {},
                "subtotal_no_margin": part_subtotal_no_margin,
                "notes": part.get("notes"),
            }
        )
        part_conf = (sum((part.get("confidence") or {}).values()) / 4.0) if part.get("confidence") else 0.0
        confidence_values.append(part_conf)

    hardware_total = 0.0
    consumables_factor_pct = _get_setting_number(db, "rfq_consumables_factor_pct", 8.0)
    for hardware in parsed["hardware_items"]:
        qty = float(hardware.get("qty") or 1.0)
        hardware_price = pricing.get_hardware_price(
            db=db,
            item_code=hardware.get("part_number"),
            description=hardware.get("part_name") or hardware.get("notes") or "",
            rfq_package_id=package.id,
            quote_estimate_id=estimate.id,
            consumables_factor_pct=consumables_factor_pct,
        )
        hardware_total += hardware_price.unit_price * qty
        if hardware_price.is_fallback:
            assumptions.append(
                {
                    "part_id": hardware.get("part_number") or hardware.get("part_name"),
                    "field": "hardware_price",
                    "assumption": hardware_price.notes or "Fallback hardware price used.",
                    "confidence": 0.5,
                }
            )

    hardware_consumables_total = hardware_total * (1.0 + consumables_factor_pct / 100.0)

    subtotal_no_margin = material_total + outside_total + shop_labor_oh_total + hardware_consumables_total
    margin_total = calc_margin(subtotal_no_margin, config.target_margin_pct)
    grand_total = subtotal_no_margin + margin_total

    for payload in line_payloads:
        proportion = (payload["subtotal_no_margin"] / subtotal_no_margin) if subtotal_no_margin > 0 else 0.0
        payload["part_total"] = payload["subtotal_no_margin"] + (margin_total * proportion)

    lead = estimate_lead_time_range(
        total_shop_hours=total_shop_hours,
        outside_service_days=max_outside_days,
        base_queue_days=config.base_queue_days,
        effective_daily_capacity_hours=config.effective_daily_capacity_hours,
        extra_outside_service_buffer_days=config.outside_service_buffer_days,
    )
    lead_min = int(lead["min_days"])
    lead_max = int(lead["max_days"])

    quote.subtotal = round(grand_total, 2)
    quote.total = round(grand_total, 2)
    quote.lead_time_days = lead_max

    for payload in line_payloads:
        line_total = payload["part_total"]
        quantity = payload["quantity"] if payload["quantity"] > 0 else 1
        quote_line = QuoteLine(
            quote_id=quote.id,
            line_number=payload["line_number"],
            part_id=None,
            description=f"{payload['part_name']} ({payload.get('material') or 'TBD'}, {payload.get('thickness') or 'TBD'})",
            quantity=quantity,
            unit_price=line_total / quantity,
            line_total=line_total,
            material_cost=0.0,
            labor_hours=0.0,
            labor_cost=0.0,
            notes="Generated by AI RFQ estimate",
        )
        db.add(quote_line)

        db.add(
            QuoteLineSummary(
                quote_estimate_id=estimate.id,
                part_number=payload.get("part_number"),
                part_name=payload.get("part_name"),
                quantity=payload.get("quantity") or 1,
                material=payload.get("material"),
                thickness=payload.get("thickness"),
                flat_area=payload.get("flat_area"),
                cut_length=payload.get("cut_length"),
                bend_count=payload.get("bend_count"),
                hole_count=payload.get("hole_count"),
                finish=payload.get("finish"),
                weld_required=payload.get("weld_required"),
                assembly_required=payload.get("assembly_required"),
                part_total=payload.get("part_total") or 0.0,
                confidence=payload.get("confidence"),
                sources=payload.get("sources"),
                notes=payload.get("notes"),
            )
        )

    overall_conf = (sum(confidence_values) / len(confidence_values)) if confidence_values else 0.0
    if parsed["missing_specs"]:
        penalty = min(0.35, 0.03 * len(parsed["missing_specs"]))
        overall_conf = max(0.0, overall_conf - penalty)

    estimate.material_total = round(material_total, 2)
    estimate.hardware_consumables_total = round(hardware_consumables_total, 2)
    estimate.outside_services_total = round(outside_total, 2)
    estimate.shop_labor_oh_total = round(shop_labor_oh_total, 2)
    estimate.margin_total = round(margin_total, 2)
    estimate.grand_total = round(grand_total, 2)
    estimate.lead_time_min_days = lead_min
    estimate.lead_time_max_days = lead_max
    estimate.lead_time_confidence = float(lead["confidence"])
    estimate.confidence_score = round(overall_conf, 3)
    estimate.confidence_detail = {
        "per_part_average": confidence_values,
        "missing_specs_count": len(parsed["missing_specs"]),
    }
    estimate.assumptions = assumptions
    estimate.missing_specs = missing_specs
    estimate.source_attribution = parsed["source_attribution"]
    estimate.internal_breakdown = {
        "total_shop_hours": round(total_shop_hours, 3),
        "outside_service_days": max_outside_days,
        "target_margin_pct": config.target_margin_pct,
    }

    package.status = "estimated"
    package.parsing_warnings = parsed["warnings"]

    db.commit()
    db.refresh(estimate)
    db.refresh(quote)

    summaries = (
        db.query(QuoteLineSummary)
        .filter(QuoteLineSummary.quote_estimate_id == estimate.id)
        .order_by(QuoteLineSummary.id.asc())
        .all()
    )

    return QuoteEstimateResponse(
        rfq_package_id=package.id,
        estimate_id=estimate.id,
        quote_id=quote.id,
        quote_number=quote.quote_number,
        totals={
            "material": estimate.material_total,
            "hardware_consumables": estimate.hardware_consumables_total,
            "outside_services": estimate.outside_services_total,
            "shop_labor_oh": estimate.shop_labor_oh_total,
            "margin": estimate.margin_total,
            "grand_total": estimate.grand_total,
        },
        lead_time={
            "label": f"{lead_min}-{lead_max} business days",
            "min_days": lead_min,
            "max_days": lead_max,
            "confidence": estimate.lead_time_confidence,
        },
        confidence={
            "overall": estimate.confidence_score,
            "details": estimate.confidence_detail,
        },
        assumptions=estimate.assumptions or [],
        missing_specs=estimate.missing_specs or [],
        source_attribution=estimate.source_attribution or {},
        line_summaries=[
            QuoteLineSummaryResponse(
                part_number=item.part_number,
                part_name=item.part_name,
                quantity=item.quantity,
                material=item.material,
                thickness=item.thickness,
                flat_area=item.flat_area,
                cut_length=item.cut_length,
                hole_count=item.hole_count,
                bend_count=item.bend_count,
                finish=item.finish,
                part_total=item.part_total,
                confidence=item.confidence or {},
                sources=item.sources or {},
            )
            for item in summaries
        ],
    )


@router.post("/{package_id}/approve-create-quote")
def approve_estimate(
    package_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
):
    package = (
        db.query(RfqPackage)
        .options(joinedload(RfqPackage.estimates))
        .filter(RfqPackage.id == package_id)
        .first()
    )
    if not package:
        raise HTTPException(status_code=404, detail="RFQ package not found")
    if not package.estimates:
        raise HTTPException(status_code=400, detail="No estimate exists for this RFQ package.")

    latest = max(package.estimates, key=lambda item: item.created_at)
    if not latest.quote_id:
        raise HTTPException(status_code=400, detail="Estimate is not linked to a quote.")
    quote = db.query(Quote).filter(Quote.id == latest.quote_id).first()
    if not quote:
        raise HTTPException(status_code=404, detail="Linked quote not found")

    quote.status = QuoteStatus.PENDING
    package.status = "approved"
    db.commit()

    return {
        "message": "Estimate approved and quote created.",
        "rfq_package_id": package.id,
        "quote_id": quote.id,
        "quote_number": quote.quote_number,
    }


@router.get("/{package_id}/internal-estimate-export")
def export_internal_estimate(
    package_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    package = (
        db.query(RfqPackage)
        .options(joinedload(RfqPackage.estimates).joinedload(QuoteEstimate.line_summaries))
        .filter(RfqPackage.id == package_id)
        .first()
    )
    if not package:
        raise HTTPException(status_code=404, detail="RFQ package not found")
    if not package.estimates:
        raise HTTPException(status_code=400, detail="No estimate found for this package")
    estimate = max(package.estimates, key=lambda item: item.created_at)

    payload = {
        "rfq_number": package.rfq_number,
        "customer_name": package.customer_name,
        "rfq_reference": package.rfq_reference,
        "quote_id": estimate.quote_id,
        "totals": {
            "material": estimate.material_total,
            "hardware_consumables": estimate.hardware_consumables_total,
            "outside_services": estimate.outside_services_total,
            "shop_labor_oh": estimate.shop_labor_oh_total,
            "margin": estimate.margin_total,
            "grand_total": estimate.grand_total,
        },
        "lead_time": {
            "min_days": estimate.lead_time_min_days,
            "max_days": estimate.lead_time_max_days,
            "confidence": estimate.lead_time_confidence,
        },
        "confidence": {
            "overall": estimate.confidence_score,
            "detail": estimate.confidence_detail,
        },
        "assumptions": estimate.assumptions or [],
        "missing_specs": estimate.missing_specs or [],
        "source_attribution": estimate.source_attribution or {},
        "internal_breakdown": estimate.internal_breakdown or {},
        "line_summaries": [
            {
                "part_number": line.part_number,
                "part_name": line.part_name,
                "quantity": line.quantity,
                "material": line.material,
                "thickness": line.thickness,
                "flat_area": line.flat_area,
                "cut_length": line.cut_length,
                "hole_count": line.hole_count,
                "bend_count": line.bend_count,
                "finish": line.finish,
                "weld_required": line.weld_required,
                "assembly_required": line.assembly_required,
                "part_total": line.part_total,
                "confidence": line.confidence,
                "sources": line.sources,
                "notes": line.notes,
            }
            for line in estimate.line_summaries
        ],
    }

    filename = f"{package.rfq_number}_internal_estimate.json"
    data = json.dumps(payload, indent=2).encode("utf-8")
    return StreamingResponse(
        BytesIO(data),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
