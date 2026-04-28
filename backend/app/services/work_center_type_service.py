import json
import re
from typing import List, Optional
from sqlalchemy.orm import Session

from app.models.quote_config import QuoteSettings
from app.models.work_center import WorkCenter


DEFAULT_WORK_CENTER_TYPES = [
    "fabrication",
    "laser",
    "press_brake",
    "cnc_machining",
    "welding",
    "assembly",
    "paint",
    "powder_coating",
    "inspection",
    "shipping",
]


def normalize_work_center_type(value: str) -> str:
    if not value:
        return ""
    val = value.strip().lower()
    val = re.sub(r"[^a-z0-9\s_-]", "", val)
    val = re.sub(r"[\s-]+", "_", val)
    return val.strip("_")


def get_work_center_types(db: Session, include_in_use: bool = True, company_id: Optional[int] = None) -> List[str]:
    setting_query = db.query(QuoteSettings).filter(QuoteSettings.setting_key == "work_center_types")
    if company_id is not None:
        setting_query = setting_query.filter(QuoteSettings.company_id == company_id)
    setting = setting_query.first()
    types: List[str] = []

    if setting and setting.setting_value:
        try:
            raw = json.loads(setting.setting_value)
            if isinstance(raw, list):
                types = [normalize_work_center_type(str(item)) for item in raw if str(item).strip()]
        except Exception:
            types = []

    if not types:
        types = DEFAULT_WORK_CENTER_TYPES.copy()

    if include_in_use:
        in_use = get_in_use_work_center_types(db, company_id=company_id)
        for wc_type in in_use:
            if wc_type and wc_type not in types:
                types.append(wc_type)

    # De-duplicate while preserving order
    seen = set()
    deduped = []
    for wc_type in types:
        if wc_type and wc_type not in seen:
            seen.add(wc_type)
            deduped.append(wc_type)

    return deduped


def get_in_use_work_center_types(db: Session, company_id: Optional[int] = None) -> List[str]:
    query = db.query(WorkCenter.work_center_type)
    if company_id is not None:
        query = query.filter(WorkCenter.company_id == company_id)
    in_use = [
        normalize_work_center_type(row[0])
        for row in query.distinct().all()
        if row and row[0]
    ]
    # De-duplicate while preserving order
    seen = set()
    result = []
    for wc_type in in_use:
        if wc_type and wc_type not in seen:
            seen.add(wc_type)
            result.append(wc_type)
    return result


def set_work_center_types(db: Session, types: List[str], company_id: Optional[int] = None) -> List[str]:
    normalized = [normalize_work_center_type(t) for t in (types or [])]
    normalized = [t for t in normalized if t]

    # De-duplicate while preserving order
    seen = set()
    result = []
    for wc_type in normalized:
        if wc_type not in seen:
            seen.add(wc_type)
            result.append(wc_type)

    setting_query = db.query(QuoteSettings).filter(QuoteSettings.setting_key == "work_center_types")
    if company_id is not None:
        setting_query = setting_query.filter(QuoteSettings.company_id == company_id)
    setting = setting_query.first()
    if setting:
        setting.setting_value = json.dumps(result)
        setting.setting_type = "json"
    else:
        setting = QuoteSettings(
            setting_key="work_center_types",
            setting_value=json.dumps(result),
            setting_type="json",
            description="Allowed work center types",
        )
        if company_id is not None:
            setting.company_id = company_id
        db.add(setting)

    return result
