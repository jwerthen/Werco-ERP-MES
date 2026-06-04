"""Laser nest package parsing and import helpers."""

from __future__ import annotations

import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderType

CNC_EXTENSIONS = {
    ".cnc",
    ".eia",
    ".fgc",
    ".lcc",
    ".mpf",
    ".nc",
    ".ncc",
    ".ord",
    ".pgm",
    ".tap",
}


@dataclass(frozen=True)
class ParsedLaserNest:
    nest_name: str
    cnc_file_name: str
    cnc_file_path: Optional[str]
    planned_runs: int
    material: Optional[str] = None
    thickness: Optional[str] = None
    sheet_size: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "nest_name": self.nest_name,
            "cnc_file_name": self.cnc_file_name,
            "cnc_file_path": self.cnc_file_path,
            "planned_runs": self.planned_runs,
            "material": self.material,
            "thickness": self.thickness,
            "sheet_size": self.sheet_size,
        }


def parse_laser_nest_folder(source_path: str) -> list[ParsedLaserNest]:
    root = Path(source_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError("Source path must be an existing folder")
    return _parse_entries(_iter_cnc_files(root))


def parse_laser_nest_zip(zip_path: str) -> list[ParsedLaserNest]:
    with TemporaryDirectory() as temp_dir:
        with zipfile.ZipFile(zip_path) as archive:
            _safe_extract_zip(archive, Path(temp_dir))
        return parse_laser_nest_folder(temp_dir)


def preview_laser_nest_package(source_path: Optional[str] = None, zip_path: Optional[str] = None) -> list[dict]:
    if zip_path:
        nests = parse_laser_nest_zip(zip_path)
    elif source_path:
        nests = parse_laser_nest_folder(source_path)
    else:
        raise ValueError("Provide a zipped package or source folder")
    return [nest.as_dict() for nest in nests]


def copy_laser_nest_folder(source_path: str, destination: str) -> None:
    source = Path(source_path).expanduser().resolve()
    dest = Path(destination).resolve()
    if not source.exists() or not source.is_dir():
        raise ValueError("Source path must be an existing folder")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)


def extract_laser_nest_zip(zip_path: str, destination: str) -> None:
    dest = Path(destination).resolve()
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        _safe_extract_zip(archive, dest)


def build_laser_nest_child_work_order(
    db: Session,
    *,
    parent_work_order: WorkOrder,
    child_work_order: WorkOrder,
    package_name: str,
    package_source_path: Optional[str],
    nests: list[ParsedLaserNest],
    laser_work_center: WorkCenter,
    company_id: int,
    created_by: Optional[int],
) -> LaserNestPackage:
    """Replace a child laser WO's nest tasks with the supplied package plan."""

    existing_packages = (
        db.query(LaserNestPackage)
        .filter(
            LaserNestPackage.company_id == company_id,
            LaserNestPackage.child_work_order_id == child_work_order.id,
        )
        .all()
    )
    for package in existing_packages:
        db.delete(package)
    db.flush()

    existing_operations = (
        db.query(WorkOrderOperation)
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.work_order_id == child_work_order.id,
            WorkOrderOperation.operation_group == "LASER",
        )
        .all()
    )
    for operation in existing_operations:
        db.delete(operation)
    db.flush()

    package = LaserNestPackage(
        company_id=company_id,
        parent_work_order_id=parent_work_order.id,
        child_work_order_id=child_work_order.id,
        package_name=package_name,
        source_path=package_source_path,
        import_status="imported",
        created_by=created_by,
    )
    db.add(package)
    db.flush()

    for index, nest in enumerate(nests, start=1):
        sequence = index * 10
        operation = WorkOrderOperation(
            company_id=company_id,
            work_order_id=child_work_order.id,
            work_center_id=laser_work_center.id,
            sequence=sequence,
            operation_number=f"Nest {index}",
            name=f"Laser Cut - {nest.nest_name}",
            description=_laser_operation_description(nest),
            component_quantity=float(nest.planned_runs),
            setup_time_hours=0.0,
            run_time_hours=0.0,
            run_time_per_piece=0.0,
            status=OperationStatus.READY if index == 1 else OperationStatus.PENDING,
            operation_group="LASER",
        )
        db.add(operation)
        db.flush()

        db.add(
            LaserNest(
                company_id=company_id,
                package_id=package.id,
                work_order_operation_id=operation.id,
                nest_name=nest.nest_name,
                cnc_file_name=nest.cnc_file_name,
                cnc_file_path=nest.cnc_file_path,
                planned_runs=nest.planned_runs,
                completed_runs=0,
                material=nest.material,
                thickness=nest.thickness,
                sheet_size=nest.sheet_size,
            )
        )

    total_runs = sum(nest.planned_runs for nest in nests)
    child_work_order.quantity_ordered = float(total_runs or 1)
    child_work_order.parent_work_order_id = parent_work_order.id
    child_work_order.work_order_type = WorkOrderType.LASER_CUTTING.value
    return package


def sync_laser_nest_from_operation(operation: WorkOrderOperation) -> None:
    if operation.laser_nest:
        operation.laser_nest.completed_runs = float(operation.quantity_complete or 0)


def _iter_cnc_files(root: Path) -> Iterable[tuple[Path, str]]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in CNC_EXTENSIONS:
            continue
        rel_path = str(path.relative_to(root))
        yield path, rel_path


def _parse_entries(entries: Iterable[tuple[Path, str]]) -> list[ParsedLaserNest]:
    nests = []
    for path, rel_path in entries:
        nests.append(_parse_filename(path.name, rel_path))
    if not nests:
        raise ValueError("No CNC files found in package")
    return nests


def _parse_filename(file_name: str, rel_path: str) -> ParsedLaserNest:
    stem = Path(file_name).stem
    cleaned = re.sub(r"[_\-]+", " ", stem).strip()
    planned_runs = _infer_planned_runs(cleaned)
    nest_name = _infer_nest_name(cleaned)
    return ParsedLaserNest(
        nest_name=nest_name,
        cnc_file_name=file_name,
        cnc_file_path=rel_path,
        planned_runs=planned_runs,
        material=_infer_material(cleaned),
        thickness=_infer_thickness(cleaned),
        sheet_size=_infer_sheet_size(cleaned),
    )


def _infer_planned_runs(text: str) -> int:
    patterns = [
        r"(?:^|\s)(?:runs?|qty|quantity|sheets?)\s*[:#-]?\s*(\d{1,4})(?:\s|$)",
        r"(?:^|\s)(\d{1,4})\s*x(?:\s|$)",
        r"(?:^|\s)x\s*(\d{1,4})(?:\s|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return max(1, int(match.group(1)))
    return 1


def _infer_nest_name(text: str) -> str:
    name = re.sub(r"(?:runs?|qty|quantity|sheets?)\s*[:#-]?\s*\d{1,4}", "", text, flags=re.IGNORECASE)
    name = re.sub(r"(?:^|\s)\d{1,4}\s*x(?:\s|$)", " ", name, flags=re.IGNORECASE)
    name = re.sub(r"(?:^|\s)x\s*\d{1,4}(?:\s|$)", " ", name, flags=re.IGNORECASE)
    name = " ".join(name.split())
    return name or text or "Laser Nest"


def _infer_material(text: str) -> Optional[str]:
    material_patterns = [
        r"\b(A36|A572|A514|AR400|AR500|SS304|SS316|304SS|316SS|AL5052|AL6061|CRS|HRS)\b",
        r"\b(Aluminum|Aluminium|Stainless|Mild Steel|Carbon Steel)\b",
    ]
    for pattern in material_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _infer_thickness(text: str) -> Optional[str]:
    match = re.search(r"\b(\d+(?:\.\d+)?\s*(?:ga|gauge|in|inch|mm))\b", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).replace(" ", "")
    return None


def _infer_sheet_size(text: str) -> Optional[str]:
    match = re.search(r"\b(\d{2,3})\s*[xX]\s*(\d{2,3})\b", text)
    if match:
        return f"{match.group(1)}x{match.group(2)}"
    return None


def _laser_operation_description(nest: ParsedLaserNest) -> str:
    parts = [f"CNC: {nest.cnc_file_name}", f"Planned runs: {nest.planned_runs}"]
    if nest.material:
        parts.append(f"Material: {nest.material}")
    if nest.thickness:
        parts.append(f"Thickness: {nest.thickness}")
    if nest.sheet_size:
        parts.append(f"Sheet: {nest.sheet_size}")
    return " | ".join(parts)


def _safe_extract_zip(archive: zipfile.ZipFile, destination: Path) -> None:
    destination = destination.resolve()
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        try:
            target.relative_to(destination)
        except ValueError as exc:
            raise ValueError("Zip package contains an unsafe path") from exc
    archive.extractall(destination)
