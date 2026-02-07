"""Pricing services for RFQ AI quoting with cache and source attribution."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Protocol, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.models.part import Part, PartType
from app.models.quote_config import MaterialCategory, QuoteMaterial, QuoteSettings
from app.models.rfq_quote import PriceSnapshot
from app.services.sheet_metal_costing_service import normalize_material


DEFAULT_MATERIAL_PRICE_PER_LB = {
    "carbon_steel": 1.10,
    "stainless": 2.80,
    "aluminum": 2.40,
}


@dataclass
class PriceResult:
    unit_price: float
    unit: str
    supplier: str
    source_name: str
    source_url: Optional[str] = None
    is_fallback: bool = False
    notes: Optional[str] = None


class MaterialPriceProvider(Protocol):
    provider_name: str

    def get_material_price(
        self, db: Session, material: str, thickness: Optional[str] = None
    ) -> Optional[PriceResult]:
        ...

    def get_hardware_price(
        self, db: Session, item_code: Optional[str], description: str
    ) -> Optional[PriceResult]:
        ...


def _get_quote_setting(db: Session, key: str, default):
    setting = db.query(QuoteSettings).filter(QuoteSettings.setting_key == key).first()
    if not setting:
        return default
    if setting.setting_type == "number":
        try:
            return float(setting.setting_value)
        except Exception:
            return default
    return setting.setting_value if setting.setting_value is not None else default


class InternalPriceListProvider:
    provider_name = "InternalPriceList"

    def _category_for_material(self, material: str) -> MaterialCategory:
        m = normalize_material(material) or "carbon_steel"
        if m == "stainless":
            return MaterialCategory.STAINLESS
        if m == "aluminum":
            return MaterialCategory.ALUMINUM
        return MaterialCategory.STEEL

    def _pick_sheet_price(
        self, sheet_pricing: Dict[str, float], thickness: Optional[str]
    ) -> Optional[float]:
        if not sheet_pricing:
            return None
        if thickness and thickness in sheet_pricing:
            return float(sheet_pricing[thickness])
        numeric_keys: List[Tuple[float, float]] = []
        for key, value in sheet_pricing.items():
            try:
                if key.endswith("ga"):
                    continue
                numeric_keys.append((abs(float(key)), float(value)))
            except Exception:
                continue
        if thickness:
            try:
                thickness_value = abs(float(thickness))
                if numeric_keys:
                    closest = min(numeric_keys, key=lambda item: abs(item[0] - thickness_value))
                    return closest[1]
            except Exception:
                pass
        first_value = next(iter(sheet_pricing.values()), None)
        return float(first_value) if first_value is not None else None

    def get_material_price(
        self, db: Session, material: str, thickness: Optional[str] = None
    ) -> Optional[PriceResult]:
        category = self._category_for_material(material)
        mat = (
            db.query(QuoteMaterial)
            .filter(QuoteMaterial.category == category, QuoteMaterial.is_active.is_(True))
            .order_by(QuoteMaterial.updated_at.desc())
            .first()
        )
        if not mat:
            return None

        price_per_lb = mat.stock_price_per_pound
        if mat.sheet_pricing:
            sheet_price = self._pick_sheet_price(mat.sheet_pricing, thickness)
            if sheet_price and mat.density_lb_per_cubic_inch and thickness:
                # Convert sheet price ($/sqft) to approximate $/lb for consistent costing.
                try:
                    thickness_in = float(thickness)
                    lbs_per_sqft = thickness_in * 144.0 * mat.density_lb_per_cubic_inch
                    if lbs_per_sqft > 0:
                        price_per_lb = sheet_price / lbs_per_sqft
                except Exception:
                    pass

        if price_per_lb <= 0:
            return None

        return PriceResult(
            unit_price=float(price_per_lb),
            unit="lb",
            supplier="Internal Catalog",
            source_name=f"QuoteMaterial:{mat.name}",
            source_url=None,
        )

    def get_hardware_price(
        self, db: Session, item_code: Optional[str], description: str
    ) -> Optional[PriceResult]:
        query = db.query(Part).filter(
            Part.part_type.in_([PartType.HARDWARE, PartType.CONSUMABLE]),
            Part.is_active.is_(True),
        )
        part = None
        if item_code:
            part = query.filter(Part.part_number == item_code).first()
        if not part and description:
            part = query.filter(Part.name.ilike(f"%{description[:40]}%")).first()
        if not part or not part.standard_cost or part.standard_cost <= 0:
            return None

        return PriceResult(
            unit_price=float(part.standard_cost),
            unit="each",
            supplier="ERP Item Master",
            source_name=f"Part:{part.part_number}",
            source_url=None,
        )


class WebLookupProvider:
    """
    Placeholder provider intentionally disabled unless a controlled web lookup is available.
    """

    provider_name = "WebLookup"

    def get_material_price(self, db: Session, material: str, thickness: Optional[str] = None) -> Optional[PriceResult]:
        return None

    def get_hardware_price(self, db: Session, item_code: Optional[str], description: str) -> Optional[PriceResult]:
        return None


class MaterialPriceService:
    def __init__(self, providers: Optional[List[MaterialPriceProvider]] = None):
        self.providers = providers or [InternalPriceListProvider(), WebLookupProvider()]

    def _cache_hours(self, db: Session) -> int:
        value = _get_quote_setting(db, "rfq_price_cache_hours", 12)
        try:
            return max(1, int(value))
        except Exception:
            return 12

    def _lookup_cached(
        self,
        db: Session,
        price_type: str,
        material: Optional[str] = None,
        thickness: Optional[str] = None,
        item_code: Optional[str] = None,
        stale_ok: bool = False,
    ) -> Optional[PriceSnapshot]:
        query = db.query(PriceSnapshot).filter(PriceSnapshot.price_type == price_type)
        if material:
            query = query.filter(PriceSnapshot.material == material)
        if thickness:
            query = query.filter(PriceSnapshot.thickness == thickness)
        if item_code:
            query = query.filter(PriceSnapshot.item_code == item_code)
        row = query.order_by(desc(PriceSnapshot.fetched_at)).first()
        if not row:
            return None
        if stale_ok:
            return row
        freshness_threshold = datetime.utcnow() - timedelta(hours=self._cache_hours(db))
        if row.fetched_at and row.fetched_at >= freshness_threshold:
            return row
        return None

    def _store_snapshot(
        self,
        db: Session,
        *,
        quote_estimate_id: Optional[int],
        rfq_package_id: Optional[int],
        scope: str,
        price_type: str,
        item_code: Optional[str],
        material: Optional[str],
        thickness: Optional[str],
        result: PriceResult,
        raw_data: Optional[dict] = None,
    ) -> PriceSnapshot:
        snapshot = PriceSnapshot(
            quote_estimate_id=quote_estimate_id,
            rfq_package_id=rfq_package_id,
            snapshot_scope=scope,
            price_type=price_type,
            item_code=item_code,
            material=material,
            thickness=thickness,
            unit=result.unit,
            unit_price=float(result.unit_price),
            currency="USD",
            supplier=result.supplier,
            source_name=result.source_name,
            source_url=result.source_url,
            is_fallback=result.is_fallback,
            notes=result.notes,
            raw_data=raw_data,
            fetched_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(hours=self._cache_hours(db)),
        )
        db.add(snapshot)
        return snapshot

    def get_material_price(
        self,
        db: Session,
        material: str,
        thickness: Optional[str],
        rfq_package_id: Optional[int],
        quote_estimate_id: Optional[int] = None,
    ) -> PriceResult:
        normalized = normalize_material(material) or "carbon_steel"

        cached = self._lookup_cached(db, "material", material=normalized, thickness=thickness)
        if cached:
            result = PriceResult(
                unit_price=float(cached.unit_price),
                unit=cached.unit,
                supplier=cached.supplier,
                source_name=f"{cached.source_name} (cached)",
                source_url=cached.source_url,
                is_fallback=cached.is_fallback,
                notes=cached.notes,
            )
            self._store_snapshot(
                db,
                quote_estimate_id=quote_estimate_id,
                rfq_package_id=rfq_package_id,
                scope="estimate",
                price_type="material",
                item_code=None,
                material=normalized,
                thickness=thickness,
                result=result,
                raw_data={"cache_hit": True},
            )
            return result

        for provider in self.providers:
            price = provider.get_material_price(db, normalized, thickness)
            if not price:
                continue
            self._store_snapshot(
                db,
                quote_estimate_id=None,
                rfq_package_id=rfq_package_id,
                scope="cache",
                price_type="material",
                item_code=None,
                material=normalized,
                thickness=thickness,
                result=price,
                raw_data={"provider": provider.provider_name},
            )
            self._store_snapshot(
                db,
                quote_estimate_id=quote_estimate_id,
                rfq_package_id=rfq_package_id,
                scope="estimate",
                price_type="material",
                item_code=None,
                material=normalized,
                thickness=thickness,
                result=price,
                raw_data={"provider": provider.provider_name},
            )
            return price

        stale = self._lookup_cached(db, "material", material=normalized, thickness=thickness, stale_ok=True)
        if stale:
            fallback_result = PriceResult(
                unit_price=float(stale.unit_price),
                unit=stale.unit,
                supplier=stale.supplier,
                source_name=f"{stale.source_name} (last known)",
                source_url=stale.source_url,
                is_fallback=True,
                notes="Live lookup failed; using last-known material price.",
            )
            self._store_snapshot(
                db,
                quote_estimate_id=quote_estimate_id,
                rfq_package_id=rfq_package_id,
                scope="estimate",
                price_type="material",
                item_code=None,
                material=normalized,
                thickness=thickness,
                result=fallback_result,
                raw_data={"fallback": "last_known"},
            )
            return fallback_result

        configured = _get_quote_setting(db, "rfq_default_material_price_per_lb", None)
        if isinstance(configured, str):
            configured = None
        price_default = None
        if isinstance(configured, dict):
            price_default = configured.get(normalized)
        if not price_default:
            price_default = DEFAULT_MATERIAL_PRICE_PER_LB.get(normalized, 1.50)

        final_result = PriceResult(
            unit_price=float(price_default),
            unit="lb",
            supplier="Assumed",
            source_name="DefaultRFQMaterialPrice",
            is_fallback=True,
            notes="No provider price available; using configured/default fallback.",
        )
        self._store_snapshot(
            db,
            quote_estimate_id=quote_estimate_id,
            rfq_package_id=rfq_package_id,
            scope="estimate",
            price_type="material",
            item_code=None,
            material=normalized,
            thickness=thickness,
            result=final_result,
            raw_data={"fallback": "default"},
        )
        return final_result

    def get_hardware_price(
        self,
        db: Session,
        item_code: Optional[str],
        description: str,
        rfq_package_id: Optional[int],
        quote_estimate_id: Optional[int] = None,
        consumables_factor_pct: float = 8.0,
    ) -> PriceResult:
        cached = self._lookup_cached(db, "hardware", item_code=item_code)
        if cached:
            result = PriceResult(
                unit_price=float(cached.unit_price),
                unit=cached.unit,
                supplier=cached.supplier,
                source_name=f"{cached.source_name} (cached)",
                source_url=cached.source_url,
                is_fallback=cached.is_fallback,
                notes=cached.notes,
            )
            self._store_snapshot(
                db,
                quote_estimate_id=quote_estimate_id,
                rfq_package_id=rfq_package_id,
                scope="estimate",
                price_type="hardware",
                item_code=item_code,
                material=None,
                thickness=None,
                result=result,
                raw_data={"cache_hit": True},
            )
            return result

        for provider in self.providers:
            price = provider.get_hardware_price(db, item_code, description)
            if not price:
                continue
            self._store_snapshot(
                db,
                quote_estimate_id=None,
                rfq_package_id=rfq_package_id,
                scope="cache",
                price_type="hardware",
                item_code=item_code,
                material=None,
                thickness=None,
                result=price,
                raw_data={"provider": provider.provider_name},
            )
            self._store_snapshot(
                db,
                quote_estimate_id=quote_estimate_id,
                rfq_package_id=rfq_package_id,
                scope="estimate",
                price_type="hardware",
                item_code=item_code,
                material=None,
                thickness=None,
                result=price,
                raw_data={"provider": provider.provider_name},
            )
            return price

        stale = self._lookup_cached(db, "hardware", item_code=item_code, stale_ok=True)
        if stale:
            fallback = PriceResult(
                unit_price=float(stale.unit_price),
                unit=stale.unit,
                supplier=stale.supplier,
                source_name=f"{stale.source_name} (last known)",
                source_url=stale.source_url,
                is_fallback=True,
                notes="Live lookup failed; using last-known hardware price.",
            )
            self._store_snapshot(
                db,
                quote_estimate_id=quote_estimate_id,
                rfq_package_id=rfq_package_id,
                scope="estimate",
                price_type="hardware",
                item_code=item_code,
                material=None,
                thickness=None,
                result=fallback,
                raw_data={"fallback": "last_known"},
            )
            return fallback

        assumed = PriceResult(
            unit_price=0.25 * (1.0 + max(consumables_factor_pct, 0.0) / 100.0),
            unit="each",
            supplier="Assumed",
            source_name="ConsumablesFactorModel",
            is_fallback=True,
            notes="Hardware item not in item master; using standard consumables factor.",
        )
        self._store_snapshot(
            db,
            quote_estimate_id=quote_estimate_id,
            rfq_package_id=rfq_package_id,
            scope="estimate",
            price_type="hardware",
            item_code=item_code,
            material=None,
            thickness=None,
            result=assumed,
            raw_data={"fallback": "consumables_factor"},
        )
        return assumed
