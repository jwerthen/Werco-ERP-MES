"""
Audit Log Retention / Archival Service.

CMMC Level 2 Control: AU-3.3.8 (Protect Audit Information) + AS9100D records control.

Audit logs are append-only and immutable: the ``tr_audit_log_no_update`` /
``tr_audit_log_no_delete`` database triggers (migration 008) reject any UPDATE or
DELETE, and the rows form a single global SHA-256 hash chain whose verifiability
depends on no rows being removed (a missing sequence number is reported as a
``sequence_gap`` tamper indicator). A maintenance job therefore must NEVER
row-delete audit logs.

This service implements the compliant alternative: it *exports* aged audit rows
(those past their retention window) to cold storage in a verifiable form, records
the export in the governance ledger (``ExportEvent``) and the audit trail itself,
and leaves the live rows in place. The hash chain is untouched and stays
verifiable. Physical removal of aged rows from the online database, if ever
required for storage, is a deliberate, documented DBA partition-drop operation
(see docs/AUDIT_LOG_RETENTION_RUNBOOK.md) — it is never automated here and never
done by disabling the immutability triggers.

Retention windows come from the per-company ``security_audit_record``
``RetentionPolicy`` row (seeded by migration 030), falling back to
``settings.AUDIT_RETENTION_DAYS_DEFAULT`` when a company has no active policy.

Idempotency: each run resumes from the last archived sequence number, read from
the most recent archival ``ExportEvent`` for the company (its "high-water mark"),
so repeated runs never re-export or skip rows.
"""

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import asc, desc
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.governance import DataClassification, ExportEvent, LegalHold, RetentionPolicy
from app.services.audit_integrity_service import AuditIntegrityService
from app.services.audit_service import AuditService

logger = get_logger(__name__)

# Governance ledger discriminators for audit-archival exports.
ARCHIVE_RECORD_TYPE = "audit_logs"
ARCHIVE_EXPORT_TYPE = "audit_retention_archive"
SECURITY_AUDIT_POLICY_KEY = "security_audit_record"


class AuditArchivalService:
    """Export aged audit rows to cold storage without ever deleting them."""

    def __init__(self, db: Session):
        self.db = db
        self.integrity = AuditIntegrityService(db)

    # ------------------------------------------------------------------ helpers

    def _resolve_retention_days(self, company_id: int) -> int:
        """
        Retention window (days) for a company's audit logs.

        Prefers the active ``security_audit_record`` RetentionPolicy row; falls
        back to ``settings.AUDIT_RETENTION_DAYS_DEFAULT`` when absent or when the
        policy carries no concrete ``default_retention_days``.
        """
        policy = (
            self.db.query(RetentionPolicy)
            .filter(
                RetentionPolicy.company_id == company_id,
                RetentionPolicy.policy_key == SECURITY_AUDIT_POLICY_KEY,
                RetentionPolicy.active.is_(True),
            )
            .first()
        )
        if policy and policy.default_retention_days and policy.default_retention_days > 0:
            return int(policy.default_retention_days)
        return int(settings.AUDIT_RETENTION_DAYS_DEFAULT)

    def _high_water_sequence(self, company_id: int) -> int:
        """Last sequence number already archived for this company (0 if none)."""
        last_export = (
            self.db.query(ExportEvent)
            .filter(
                ExportEvent.company_id == company_id,
                ExportEvent.record_type == ARCHIVE_RECORD_TYPE,
                ExportEvent.export_type == ARCHIVE_EXPORT_TYPE,
            )
            .order_by(desc(ExportEvent.id))
            .first()
        )
        if not last_export or not last_export.included_record_refs:
            return 0
        try:
            return int(last_export.included_record_refs.get("last_sequence") or 0)
        except (AttributeError, TypeError, ValueError):
            return 0

    @staticmethod
    def _serialize_row(row: AuditLog) -> Dict[str, Any]:
        """Full, lossless dict for one audit row (preserves chain fields)."""
        return {
            "id": row.id,
            "sequence_number": row.sequence_number,
            "integrity_hash": row.integrity_hash,
            "previous_hash": row.previous_hash,
            "timestamp": row.timestamp.isoformat() if row.timestamp else None,
            "user_id": row.user_id,
            "user_email": row.user_email,
            "user_name": row.user_name,
            "company_id": row.company_id,
            "action": row.action,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "resource_identifier": row.resource_identifier,
            "description": row.description,
            "old_values": row.old_values,
            "new_values": row.new_values,
            "ip_address": row.ip_address,
            "user_agent": row.user_agent,
            "session_id": row.session_id,
            "success": row.success,
            "error_message": row.error_message,
            "extra_data": row.extra_data,
        }

    def _verify_segment(self, rows: List[AuditLog]) -> List[int]:
        """
        Verify each row's content hash before archiving.

        Returns the list of sequence numbers that fail verification (empty == all
        good). We archive only a known-good segment so cold storage never receives
        rows we cannot vouch for; a non-empty result aborts the run and surfaces a
        tamper signal.
        """
        bad: List[int] = []
        for row in rows:
            is_valid, _issue = self.integrity.verify_single_record(row)
            if not is_valid:
                bad.append(row.sequence_number)
        return bad

    @staticmethod
    def _ensure_archive_dir_writable() -> None:
        """
        Fail loudly if the cold-storage destination cannot be written.

        Checked once up front (for non-dry runs) so a misconfigured or unmounted
        ``AUDIT_ARCHIVE_DIR`` surfaces as a visible job failure instead of being
        swallowed into per-company errors. The default path is an explicit absolute
        location that ops must provision/mount.
        """
        base = Path(settings.AUDIT_ARCHIVE_DIR)
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = base / ".write_probe"
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as exc:
            raise RuntimeError(
                f"AUDIT_ARCHIVE_DIR is not writable: {settings.AUDIT_ARCHIVE_DIR!r} ({exc}). "
                "Point AUDIT_ARCHIVE_DIR at a writable, mounted cold-storage path or set "
                "AUDIT_ARCHIVE_ENABLED=false. See docs/AUDIT_LOG_RETENTION_RUNBOOK.md."
            ) from exc

    # ------------------------------------------------------------------ per company

    def archive_company(
        self,
        company_id: int,
        as_of: Optional[datetime] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Archive one company's audit rows that are older than its retention window.

        Non-destructive: rows are exported and recorded, never deleted. Returns a
        summary dict (always includes ``status`` and ``archived_count``).
        """
        as_of = as_of or datetime.utcnow()
        retention_days = self._resolve_retention_days(company_id)
        cutoff = as_of - timedelta(days=retention_days)
        high_water = self._high_water_sequence(company_id)
        max_rows = int(settings.AUDIT_ARCHIVE_MAX_ROWS_PER_RUN)

        base = {
            "company_id": company_id,
            "retention_days": retention_days,
            "cutoff": cutoff.isoformat(),
            "high_water_sequence": high_water,
        }

        rows: List[AuditLog] = (
            self.db.query(AuditLog)
            .filter(
                AuditLog.company_id == company_id,
                AuditLog.sequence_number > high_water,
                AuditLog.timestamp < cutoff,
            )
            .order_by(asc(AuditLog.sequence_number))
            .limit(max_rows)
            .all()
        )

        if not rows:
            return {**base, "status": "nothing_to_archive", "archived_count": 0}

        # Refuse to archive a tampered segment.
        bad_sequences = self._verify_segment(rows)
        if bad_sequences:
            logger.error(
                "Audit archival aborted for company %s: %d rows failed integrity " "verification (sequences: %s...)",
                company_id,
                len(bad_sequences),
                bad_sequences[:10],
            )
            return {
                **base,
                "status": "integrity_failed",
                "archived_count": 0,
                "failed_sequences": bad_sequences,
            }

        first_row, last_row = rows[0], rows[-1]
        truncated = len(rows) == max_rows
        legal_holds = (
            self.db.query(LegalHold)
            .filter(
                LegalHold.company_id == company_id,
                LegalHold.record_type == ARCHIVE_RECORD_TYPE,
                LegalHold.active.is_(True),
            )
            .count()
        )

        # Serialize to NDJSON (one row per line) and hash the exact bytes.
        payload = "\n".join(json.dumps(self._serialize_row(r), sort_keys=True, default=str) for r in rows) + "\n"
        payload_bytes = payload.encode("utf-8")
        content_sha256 = hashlib.sha256(payload_bytes).hexdigest()

        refs = {
            "first_sequence": first_row.sequence_number,
            "last_sequence": last_row.sequence_number,
            "first_id": first_row.id,
            "last_id": last_row.id,
            "count": len(rows),
            "first_timestamp": first_row.timestamp.isoformat() if first_row.timestamp else None,
            "last_timestamp": last_row.timestamp.isoformat() if last_row.timestamp else None,
            "cutoff": cutoff.isoformat(),
            "retention_days": retention_days,
            "truncated": truncated,
        }

        summary = {
            **base,
            "status": "archived",
            "archived_count": len(rows),
            "first_sequence": first_row.sequence_number,
            "last_sequence": last_row.sequence_number,
            "content_sha256": content_sha256,
            "integrity_verified": True,
            "active_legal_holds": legal_holds,
            "truncated": truncated,
        }

        if dry_run:
            summary["status"] = "dry_run"
            return summary

        # 1. Write the cold-storage copy BEFORE the DB ledger: the safer failure
        #    mode is an orphan file (data present, ledger missing) rather than a
        #    ledger row pointing at a missing file. The filename is deterministic in
        #    the sequence range (no timestamp) — the high-water mark only advances on
        #    a successful commit, so a retry after a failed commit re-archives the
        #    same range and OVERWRITES the orphan idempotently instead of leaving a
        #    second copy. Distinct successful runs always cover disjoint ranges.
        archive_dir = Path(settings.AUDIT_ARCHIVE_DIR) / f"company_{company_id}"
        archive_dir.mkdir(parents=True, exist_ok=True)
        filename = (
            f"audit_archive_company{company_id}" f"_seq{first_row.sequence_number}-{last_row.sequence_number}.ndjson"
        )
        archive_path = archive_dir / filename
        archive_path.write_bytes(payload_bytes)
        summary["archive_path"] = str(archive_path)

        # 2. Record the export in the governance ledger (also the high-water mark).
        export_event = ExportEvent(
            company_id=company_id,
            record_type=ARCHIVE_RECORD_TYPE,
            record_id=last_row.id,
            export_type=ARCHIVE_EXPORT_TYPE,
            export_format="ndjson",
            data_classification=DataClassification.CUI.value,
            included_record_refs=refs,
            export_reason="Audit log retention archival to cold storage (CMMC AU-3.3.8)",
            exported_by=None,
            exported_at=as_of,
            destination_type="filesystem",
            destination_reference=str(archive_path),
            content_sha256=content_sha256,
            extra_data={
                "integrity_verified": True,
                "active_legal_holds": legal_holds,
                "engine": "AuditArchivalService",
                "non_destructive": True,
            },
        )
        self.db.add(export_event)

        # 3. Record the archival action in the audit trail itself (stamped with the
        #    company being archived via the per-call company_id override). This new
        #    row sequences after the archived segment, so it is not part of it.
        AuditService(self.db, user=None).log(
            action="EXPORT",
            resource_type=ARCHIVE_RECORD_TYPE,
            resource_id=last_row.id,
            resource_identifier=f"seq {first_row.sequence_number}-{last_row.sequence_number}",
            description=(
                f"Archived {len(rows)} audit log rows "
                f"(seq {first_row.sequence_number}-{last_row.sequence_number}) to cold storage"
            ),
            extra_data={
                "export_type": ARCHIVE_EXPORT_TYPE,
                "content_sha256": content_sha256,
                "destination_reference": str(archive_path),
                "retention_days": retention_days,
                "non_destructive": True,
            },
            company_id=company_id,
        )

        self.db.commit()
        logger.info(
            "Archived %d audit rows for company %s (seq %s-%s) -> %s",
            len(rows),
            company_id,
            first_row.sequence_number,
            last_row.sequence_number,
            archive_path,
        )
        return summary

    # ------------------------------------------------------------------ all companies

    def archive_all(self, as_of: Optional[datetime] = None, dry_run: bool = False) -> Dict[str, Any]:
        """
        Archive aged audit rows for every active company.

        Per-company failures are isolated (logged and collected) so one company's
        error cannot abort the others or, crucially, trigger any rollback that
        touches audit data.
        """
        as_of = as_of or datetime.utcnow()
        if not settings.AUDIT_ARCHIVE_ENABLED:
            logger.info("Audit archival disabled (AUDIT_ARCHIVE_ENABLED=false); skipping")
            return {"status": "disabled", "total_archived": 0, "companies": []}

        # Verify cold storage up front (non-dry runs) so a misconfigured or
        # unmounted destination fails the whole job loudly rather than being buried
        # in per-company errors. Raises RuntimeError -> the job wrapper logs failure.
        if not dry_run:
            self._ensure_archive_dir_writable()

        companies = self.db.query(Company).filter(Company.is_active.is_(True)).order_by(asc(Company.id)).all()

        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        total_archived = 0

        for company in companies:
            try:
                result = self.archive_company(company.id, as_of=as_of, dry_run=dry_run)
                results.append(result)
                total_archived += int(result.get("archived_count", 0))
            except Exception as exc:  # noqa: BLE001 - isolate one tenant's failure
                self.db.rollback()
                logger.error("Audit archival failed for company %s: %s", company.id, exc)
                errors.append({"company_id": company.id, "error": str(exc)})

        return {
            "status": "completed" if not errors else "completed_with_errors",
            "dry_run": dry_run,
            "as_of": as_of.isoformat(),
            "total_archived": total_archived,
            "companies": results,
            "errors": errors,
        }
