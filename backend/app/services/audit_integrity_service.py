"""
Audit Log Integrity Verification Service

CMMC Level 2 Control: AU-3.3.8 - Protect Audit Information

This service provides:
- Hash chain verification to detect tampering
- Sequence gap detection
- Integrity reports for compliance audits

Legacy/paused rows: any row whose ``integrity_hash`` carries the ``LEGACY_`` prefix is
SKIPPED by every check here rather than asserted correct. That covers both
pre-integrity-tracking rows (migration 008 backfill) and rows written while the hash
chain was paused via ``AUDIT_HASH_CHAIN_ENABLED=false`` (placeholder
``LEGACY_CHAIN_PAUSED``). Gaps touching such a row are counted in
``IntegrityReport.legacy_sequence_gaps`` instead of being reported as ``sequence_gap``
issues, because the paused-mode sequence allocator burns values on rolled-back
transactions. Consequence to state plainly: across a paused window this service offers
no tamper evidence — the DB-level immutability triggers (migrations 008/060), which no
setting can disable, are what protect those rows. See
``docs/AUDIT_LOG_RETENTION_RUNBOOK.md`` -> Pausing the hash chain.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import asc
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time_utils import to_utc_iso
from app.models.audit_log import AuditLog
from app.services.audit_service import PAUSED_CHAIN_PLACEHOLDER, compute_audit_hash

logger = get_logger(__name__)


@dataclass
class IntegrityIssue:
    """Represents an integrity violation found during verification."""

    sequence_number: int
    issue_type: str  # 'hash_mismatch', 'chain_break', 'sequence_gap', 'missing_hash'
    description: str
    record_id: int
    expected_value: Optional[str] = None
    actual_value: Optional[str] = None


@dataclass
class IntegrityReport:
    """Complete integrity verification report."""

    verified_at: datetime
    total_records: int
    records_checked: int
    first_sequence: int
    last_sequence: int
    chain_valid: bool
    issues: List[IntegrityIssue]
    legacy_records: int  # Records from before integrity tracking, or written while paused
    # Sequence gaps that fall inside a legacy/paused segment. Reported separately
    # rather than as issues: while the hash chain is paused, sequence_number comes
    # from a Postgres sequence whose values are consumed even by rolled-back
    # transactions, so gaps there are expected and are NOT evidence of tampering.
    # A non-zero value here means gap-based deletion detection does not apply
    # across that span. Defaulted so existing constructions stay valid.
    legacy_sequence_gaps: int = 0

    @property
    def is_valid(self) -> bool:
        return len(self.issues) == 0

    def to_dict(self) -> Dict:
        return {
            "verified_at": to_utc_iso(self.verified_at),
            "total_records": self.total_records,
            "records_checked": self.records_checked,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "chain_valid": self.chain_valid,
            "is_valid": self.is_valid,
            "legacy_records": self.legacy_records,
            "legacy_sequence_gaps": self.legacy_sequence_gaps,
            "issue_count": len(self.issues),
            "issues": [
                {
                    "sequence_number": i.sequence_number,
                    "issue_type": i.issue_type,
                    "description": i.description,
                    "record_id": i.record_id,
                    "expected_value": i.expected_value,
                    "actual_value": i.actual_value,
                }
                for i in self.issues
            ],
        }


class AuditIntegrityService:
    """
    Service for verifying audit log integrity.

    Performs:
    1. Hash chain verification - each record's hash must match computed hash
    2. Chain link verification - each record's previous_hash must match prior record
    3. Sequence gap detection - no gaps in sequence numbers
    """

    def __init__(self, db: Session):
        self.db = db

    def verify_single_record(self, record: AuditLog) -> Tuple[bool, Optional[IntegrityIssue]]:
        """
        Verify the integrity hash of a single audit record.

        Returns: (is_valid, issue_if_any)
        """
        # Skip legacy records (they have placeholder hashes)
        if record.integrity_hash and record.integrity_hash.startswith('LEGACY_'):
            return True, None

        # Compute what the hash should be
        expected_hash = compute_audit_hash(
            sequence_number=record.sequence_number,
            timestamp=record.timestamp,
            user_id=record.user_id,
            user_email=record.user_email,
            action=record.action,
            resource_type=record.resource_type,
            resource_id=record.resource_id,
            resource_identifier=record.resource_identifier,
            description=record.description,
            old_values=record.old_values,
            new_values=record.new_values,
            ip_address=record.ip_address,
            session_id=record.session_id,
            success=record.success,
            previous_hash=record.previous_hash,
        )

        if record.integrity_hash != expected_hash:
            return False, IntegrityIssue(
                sequence_number=record.sequence_number,
                issue_type='hash_mismatch',
                description='Record hash does not match computed hash - possible tampering',
                record_id=record.id,
                expected_value=expected_hash,
                actual_value=record.integrity_hash,
            )

        return True, None

    def verify_chain_link(
        self, current: AuditLog, previous: Optional[AuditLog]
    ) -> Tuple[bool, Optional[IntegrityIssue]]:
        """
        Verify that current record's previous_hash matches prior record's integrity_hash.

        Returns: (is_valid, issue_if_any)
        """
        # Skip chain verification for legacy records — including the CURRENT one.
        # Without this, the first row written while the hash chain was paused
        # (integrity_hash = 'LEGACY_CHAIN_PAUSED', previous_hash = NULL) is reported
        # as a chain_break by GET /audit/integrity/record/{seq}, which calls this
        # method unconditionally. The `previous` check below is not enough: it only
        # covers the row AFTER a legacy row, not the legacy row itself.
        if current.integrity_hash and current.integrity_hash.startswith('LEGACY_'):
            return True, None

        # First record has no previous
        if current.sequence_number == 1:
            if current.previous_hash is not None:
                return False, IntegrityIssue(
                    sequence_number=current.sequence_number,
                    issue_type='chain_break',
                    description='First record should have null previous_hash',
                    record_id=current.id,
                    expected_value='null',
                    actual_value=current.previous_hash,
                )
            return True, None

        if previous is None:
            return False, IntegrityIssue(
                sequence_number=current.sequence_number,
                issue_type='chain_break',
                description='Previous record not found for chain verification',
                record_id=current.id,
            )

        # Skip chain verification for legacy records
        if previous.integrity_hash and previous.integrity_hash.startswith('LEGACY_'):
            return True, None

        if current.previous_hash != previous.integrity_hash:
            return False, IntegrityIssue(
                sequence_number=current.sequence_number,
                issue_type='chain_break',
                description='Previous hash does not match prior record - chain integrity broken',
                record_id=current.id,
                expected_value=previous.integrity_hash,
                actual_value=current.previous_hash,
            )

        return True, None

    def verify_full_chain(
        self, start_sequence: Optional[int] = None, end_sequence: Optional[int] = None, batch_size: int = 1000
    ) -> IntegrityReport:
        """
        Verify the entire audit log chain (or a range).

        Args:
            start_sequence: Starting sequence number (default: 1)
            end_sequence: Ending sequence number (default: latest)
            batch_size: Number of records to process at a time

        Returns: IntegrityReport with all findings
        """
        issues = []
        legacy_count = 0
        records_checked = 0
        chain_valid = True
        # Gaps that fall inside (or on the boundary of) a legacy/paused segment.
        # Counted rather than reported as issues — see the gap check below.
        legacy_gaps = 0

        # Get total count and range
        total_records = self.db.query(AuditLog).count()

        if total_records == 0:
            return IntegrityReport(
                verified_at=datetime.utcnow(),
                total_records=0,
                records_checked=0,
                first_sequence=0,
                last_sequence=0,
                chain_valid=True,
                issues=[],
                legacy_records=0,
            )

        # Build query
        query = self.db.query(AuditLog).order_by(asc(AuditLog.sequence_number))

        if start_sequence:
            query = query.filter(AuditLog.sequence_number >= start_sequence)
        if end_sequence:
            query = query.filter(AuditLog.sequence_number <= end_sequence)

        # Get first and last sequence numbers
        first_record = query.first()
        last_record = query.order_by(AuditLog.sequence_number.desc()).first()

        first_seq = first_record.sequence_number if first_record else 0
        last_seq = last_record.sequence_number if last_record else 0

        # Process in batches
        previous_record = None
        expected_sequence = first_seq
        previous_was_legacy = False

        # If not starting from 1, get the previous record for chain verification
        if start_sequence and start_sequence > 1:
            previous_record = self.db.query(AuditLog).filter(AuditLog.sequence_number == start_sequence - 1).first()
            expected_sequence = start_sequence
            previous_was_legacy = bool(
                previous_record
                and previous_record.integrity_hash
                and previous_record.integrity_hash.startswith('LEGACY_')
            )

        offset = 0
        while True:
            batch = query.offset(offset).limit(batch_size).all()
            if not batch:
                break

            for record in batch:
                records_checked += 1
                record_is_legacy = bool(record.integrity_hash and record.integrity_hash.startswith('LEGACY_'))

                # Check for sequence gaps.
                #
                # Legacy-aware, and this is load-bearing rather than cosmetic. Gap
                # detection is the ONE check that does not already skip legacy rows,
                # and while the hash chain is paused sequence_number comes from a
                # Postgres sequence. nextval() does not roll back with the caller's
                # transaction, so a gap is the NORMAL, expected result of any rolled-back
                # request (a plain GET can roll back an audit row). Without this branch
                # every verify run would report sequence_gap issues forever and flip
                # chain_valid to false permanently — false tampering alarms that would
                # train the reader to ignore the endpoint.
                #
                # Be clear about what this costs: across a paused window, deletion
                # detection via gaps is genuinely GONE. The DB-level immutability
                # triggers (migrations 008/060) remain the protection against deletes.
                if record.sequence_number != expected_sequence:
                    gap_spans_legacy = record_is_legacy or previous_was_legacy
                    if not gap_spans_legacy:
                        chain_valid = False
                        issues.append(
                            IntegrityIssue(
                                sequence_number=expected_sequence,
                                issue_type='sequence_gap',
                                description=(
                                    f'Missing sequence number(s) between '
                                    f'{expected_sequence - 1} and {record.sequence_number}'
                                ),
                                record_id=record.id,
                                expected_value=str(expected_sequence),
                                actual_value=str(record.sequence_number),
                            )
                        )
                    else:
                        legacy_gaps += 1
                    expected_sequence = record.sequence_number

                previous_was_legacy = record_is_legacy

                # Count legacy records
                if record.integrity_hash and record.integrity_hash.startswith('LEGACY_'):
                    legacy_count += 1
                else:
                    # Verify record hash
                    is_valid, issue = self.verify_single_record(record)
                    if not is_valid and issue:
                        chain_valid = False
                        issues.append(issue)

                    # Verify chain link
                    if previous_record:
                        is_valid, issue = self.verify_chain_link(record, previous_record)
                        if not is_valid and issue:
                            chain_valid = False
                            issues.append(issue)

                previous_record = record
                expected_sequence += 1

            offset += batch_size

            # Log progress for large verifications
            if records_checked % 10000 == 0:
                logger.info(f"Audit integrity check progress: {records_checked}/{total_records}")

        return IntegrityReport(
            verified_at=datetime.utcnow(),
            total_records=total_records,
            records_checked=records_checked,
            first_sequence=first_seq,
            last_sequence=last_seq,
            chain_valid=chain_valid,
            issues=issues,
            legacy_records=legacy_count,
            legacy_sequence_gaps=legacy_gaps,
        )

    def verify_recent(self, count: int = 100) -> IntegrityReport:
        """
        Verify the most recent N audit records.
        Useful for quick health checks.
        """
        last_record = self.db.query(AuditLog).order_by(AuditLog.sequence_number.desc()).first()

        if not last_record:
            return IntegrityReport(
                verified_at=datetime.utcnow(),
                total_records=0,
                records_checked=0,
                first_sequence=0,
                last_sequence=0,
                chain_valid=True,
                issues=[],
                legacy_records=0,
            )

        start_seq = max(1, last_record.sequence_number - count + 1)
        return self.verify_full_chain(start_sequence=start_seq)

    def get_chain_status(self) -> Dict:
        """
        Get a quick status of the audit log chain.
        Returns basic stats without full verification.
        """
        total = self.db.query(AuditLog).count()

        if total == 0:
            return {
                "status": "empty",
                "total_records": 0,
                "legacy_records": 0,
                "protected_records": 0,
                "first_sequence": None,
                "last_sequence": None,
            }

        first = self.db.query(AuditLog).order_by(asc(AuditLog.sequence_number)).first()
        last = self.db.query(AuditLog).order_by(AuditLog.sequence_number.desc()).first()

        legacy_count = self.db.query(AuditLog).filter(AuditLog.integrity_hash.like('LEGACY_%')).count()
        # Rows written while the hash chain was PAUSED, counted separately from
        # migration 008's pre-chain backfill. Both are 'LEGACY_'-prefixed and both
        # land in legacy_records, but they answer different questions: 008 rows are
        # "older than the chain", paused rows are "the chain was deliberately off".
        # An assessor asking "was the chain enabled over the period under review?"
        # needs the second number specifically, so do not make them infer it.
        paused_count = self.db.query(AuditLog).filter(AuditLog.integrity_hash == PAUSED_CHAIN_PLACEHOLDER).count()

        # NOTE: has_gaps is a naive count-vs-range comparison and is deliberately
        # NOT legacy-aware, unlike verify_full_chain. It reads True across any
        # paused window — including from migration 077's start margin alone — and
        # was already True for any historical rolled-back-transaction gap. Treat it
        # as a cheap statistical probe, not a tamper signal; /integrity/verify is
        # the authority. Do not wire an alert to this field.
        has_gaps = total != (last.sequence_number - first.sequence_number + 1) if first and last else False

        return {
            "status": "active",
            "total_records": total,
            "legacy_records": legacy_count,
            "paused_records": paused_count,
            "protected_records": total - legacy_count,
            "first_sequence": first.sequence_number if first else None,
            "last_sequence": last.sequence_number if last else None,
            "expected_count": (last.sequence_number - first.sequence_number + 1) if first and last else 0,
            "has_gaps": has_gaps,
            "has_gaps_is_legacy_aware": False,
        }


def get_audit_integrity_service(db: Session) -> AuditIntegrityService:
    """Factory function for dependency injection."""
    return AuditIntegrityService(db)
