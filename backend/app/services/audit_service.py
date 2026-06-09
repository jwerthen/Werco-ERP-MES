"""
Comprehensive Audit Service for AS9100D and CMMC Level 2 Compliance.
Provides centralized audit logging for all entity changes with tamper detection.

CMMC Level 2 Control: AU-3.3.8 - Protect Audit Information
- Immutable audit logs with hash chain integrity
- Sequence numbers for gap detection
- SHA-256 cryptographic hashing
"""

import hashlib
import json
import zlib
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from fastapi import Request
from sqlalchemy import desc, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging import get_correlation_id, get_logger
from app.models.audit_log import AuditLog
from app.models.user import User

logger = get_logger(__name__)

# Transaction-level advisory lock key for the GLOBAL audit-log hash chain.
# ``sequence_number`` is globally unique across all tenants (one chain for the
# whole table), so a single fixed key serializes every audited writer's
# allocate+insert critical section. Derived deterministically from a stable
# namespace string and clamped to a signed 64-bit range for
# ``pg_advisory_xact_lock(bigint)``.
_AUDIT_CHAIN_LOCK_KEY = (zlib.crc32(b"audit_log_chain") & 0x7FFFFFFF) - 0x40000000

# Bounded retries for the residual sequence_number collision (the window where
# the advisory lock is absent, e.g. SQLite in tests, or a genuinely concurrent
# insert). Each retry re-reads the chain tail so the chain stays contiguous.
_MAX_SEQUENCE_RETRIES = 5


def compute_audit_hash(
    sequence_number: int,
    timestamp: datetime,
    user_id: Optional[int],
    user_email: Optional[str],
    action: str,
    resource_type: str,
    resource_id: Optional[int],
    resource_identifier: Optional[str],
    description: Optional[str],
    old_values: Optional[Dict],
    new_values: Optional[Dict],
    ip_address: Optional[str],
    session_id: Optional[str],
    success: str,
    previous_hash: Optional[str],
) -> str:
    """
    Compute SHA-256 hash for audit log integrity verification.

    The hash includes:
    - All significant audit fields
    - Previous record's hash (chain link)
    - Sequence number

    This creates a blockchain-like structure where tampering with
    any record breaks the chain from that point forward.

    NOTE: ``company_id`` is intentionally NOT part of the hash input. It is
    tenant-routing metadata derived from the authenticated session at write
    time (see ``AuditService._resolve_company_id``), not user-supplied content.
    Three reasons it stays out of the hash:
      1. Audit rows are already immutable at the database level via the
         ``tr_audit_log_no_update`` / ``tr_audit_log_no_delete`` triggers
         (migration 008), so ``company_id`` cannot be altered post-insert.
      2. Every row written before tenant tagging — including the rows that
         migration 026 backfilled to ``company_id = 1`` — was hashed without
         it. Adding it here would change the recomputed hash of every existing
         record, failing verification and breaking the chain wholesale.
      3. Keeping it out means ``company_id`` can be safely backfilled later
         without invalidating any integrity hash.
    Tenant isolation of audit data is therefore enforced at the query layer
    (the retrieval endpoints filter by ``company_id``), not in the hash.
    """
    # Create deterministic string representation
    hash_input = {
        "seq": sequence_number,
        "ts": timestamp.isoformat() if timestamp else None,
        "uid": user_id,
        "email": user_email,
        "action": action,
        "rtype": resource_type,
        "rid": resource_id,
        "rident": resource_identifier,
        "desc": description,
        "old": old_values,
        "new": new_values,
        "ip": ip_address,
        "sid": session_id,
        "success": success,
        "prev": previous_hash,
    }

    # Use JSON with sorted keys for deterministic serialization
    hash_string = json.dumps(hash_input, sort_keys=True, default=str)

    return hashlib.sha256(hash_string.encode('utf-8')).hexdigest()


class AuditService:
    """
    Centralized audit logging service for AS9100D compliance.

    Usage:
        audit = AuditService(db, current_user, request)
        audit.log_create("part", part.id, part.part_number, new_values=part_dict)
        audit.log_update("work_order", wo.id, wo.work_order_number, old_values, new_values)
        audit.log_delete("bom", bom.id, bom_identifier)
    """

    # Actions that require audit logging
    ACTIONS = {
        "CREATE": "CREATE",
        "UPDATE": "UPDATE",
        "DELETE": "DELETE",
        "RESTORE": "RESTORE",
        "VIEW": "VIEW",
        "EXPORT": "EXPORT",
        "IMPORT": "IMPORT",
        "LOGIN": "LOGIN",
        "LOGOUT": "LOGOUT",
        "LOGIN_FAILED": "LOGIN_FAILED",
        "PASSWORD_CHANGE": "PASSWORD_CHANGE",
        "ROLE_CHANGE": "ROLE_CHANGE",
        "STATUS_CHANGE": "STATUS_CHANGE",
        "APPROVE": "APPROVE",
        "REJECT": "REJECT",
        "RELEASE": "RELEASE",
        "COMPLETE": "COMPLETE",
        "CANCEL": "CANCEL",
    }

    # Resource types for categorization
    RESOURCE_TYPES = {
        "part": "part",
        "work_order": "work_order",
        "work_order_operation": "work_order_operation",
        "bom": "bom",
        "bom_line": "bom_line",
        "routing": "routing",
        "routing_operation": "routing_operation",
        "user": "user",
        "customer": "customer",
        "vendor": "vendor",
        "purchase_order": "purchase_order",
        "purchase_order_line": "purchase_order_line",
        "receipt": "receipt",
        "inventory": "inventory",
        "quality_record": "quality_record",
        "calibration": "calibration",
        "document": "document",
        "quote": "quote",
        "shipment": "shipment",
        "time_entry": "time_entry",
        "authentication": "authentication",
        "system": "system",
    }

    def __init__(
        self,
        db: Session,
        user: Optional[User] = None,
        request: Optional[Request] = None,
        company_id: Optional[int] = None,
    ):
        self.db = db
        self.user = user
        self.request = request
        # Tenant tag applied to every emitted audit row. Resolved once here so
        # the ~25 call sites that build an AuditService need no changes.
        self.company_id = self._resolve_company_id(user, company_id)
        self._ip_address = self._get_ip_address()
        self._user_agent = self._get_user_agent()

    @staticmethod
    def _resolve_company_id(user: Optional[User], explicit: Optional[int] = None) -> Optional[int]:
        """
        Determine which company an audit row should be tagged with.

        Precedence:
          1. An explicit ``company_id`` passed by the caller.
          2. The active company context attached by ``get_current_user``
             (``user._active_company_id``) — this is the company a platform
             admin has switched into, and matches how every other write is
             scoped via ``get_current_company_id``.
          3. The user's home company, for code paths that construct a ``User``
             outside the request dependencies (login, background jobs).
        Returns ``None`` for unauthenticated/system events (e.g. a failed
        login with no matching user), which cannot be attributed to a tenant.
        """
        if explicit is not None:
            return explicit
        if user is None:
            return None
        active = getattr(user, "_active_company_id", None)
        if active is not None:
            return active
        return getattr(user, "company_id", None)

    def _get_ip_address(self) -> Optional[str]:
        """Extract IP address from request."""
        if not self.request:
            return None
        # Check for forwarded headers (behind proxy)
        forwarded = self.request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        if self.request.client:
            return self.request.client.host
        return None

    def _get_user_agent(self) -> Optional[str]:
        """Extract user agent from request."""
        if not self.request:
            return None
        return self.request.headers.get("user-agent", "")[:500]

    def _serialize_value(self, value: Any) -> Any:
        """Serialize a value for JSON storage."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, (int, float, str, bool)):
            return value
        if isinstance(value, (list, tuple)):
            return [self._serialize_value(v) for v in value]
        if isinstance(value, dict):
            return {k: self._serialize_value(v) for k, v in value.items()}
        # For SQLAlchemy models or other objects
        return str(value)

    def _model_to_dict(self, model: Any, exclude_fields: set = None) -> Dict:
        """Convert SQLAlchemy model to dictionary."""
        if model is None:
            return {}

        exclude = exclude_fields or {"hashed_password", "password"}

        if hasattr(model, "__table__"):
            return {
                c.key: self._serialize_value(getattr(model, c.key))
                for c in inspect(model).mapper.column_attrs
                if c.key not in exclude
            }
        elif isinstance(model, dict):
            return {k: self._serialize_value(v) for k, v in model.items() if k not in exclude}
        return {}

    def _get_changes(self, old_values: Dict, new_values: Dict) -> Dict:
        """Get only the changed fields between old and new values."""
        changes = {}
        all_keys = set(old_values.keys()) | set(new_values.keys())

        for key in all_keys:
            old_val = old_values.get(key)
            new_val = new_values.get(key)
            if old_val != new_val:
                changes[key] = {"old": old_val, "new": new_val}

        return changes

    def _acquire_chain_lock(self) -> None:
        """
        Serialize the global audit-chain allocate+insert critical section.

        Acquires a transaction-level Postgres advisory lock keyed to the single
        global chain (``sequence_number`` is globally unique across all tenants).
        Held until the caller's transaction ends (commit/rollback), it guarantees
        only one writer at a time reads the tail, allocates the next sequence, and
        inserts — eliminating the read-the-same-max race under concurrency.

        Guarded by dialect: only emitted on PostgreSQL. On SQLite (the test
        backend) ``pg_advisory_xact_lock`` does not exist, and SQLite already
        serializes writers, so this is a no-op there. The savepoint/retry path in
        ``log()`` still covers any residual collision on either backend.
        """
        bind = self.db.get_bind()
        dialect = bind.dialect.name if bind is not None else ""
        if dialect != "postgresql":
            return
        self.db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _AUDIT_CHAIN_LOCK_KEY})

    def _get_next_sequence_and_previous_hash(self) -> Tuple[int, Optional[str]]:
        """
        Get the next sequence number and previous hash for chain integrity.

        Reads the current chain tail (the row with the highest
        ``sequence_number``) and returns ``(tail.sequence_number + 1,
        tail.integrity_hash)`` so the new row is contiguous and links to the
        latest hash. Returns ``(1, None)`` for the first row.

        This is the read half of the allocate+insert critical section; it must
        be called under the advisory lock acquired by ``_acquire_chain_lock``
        (PostgreSQL) and re-called on each savepoint retry so the chain stays
        correct after a residual collision.
        """
        # Get the last audit log entry
        last_entry = self.db.query(AuditLog).order_by(desc(AuditLog.sequence_number)).first()

        if last_entry:
            return last_entry.sequence_number + 1, last_entry.integrity_hash
        else:
            # First entry in the audit log
            return 1, None

    def log(
        self,
        action: str,
        resource_type: str,
        resource_id: Optional[int] = None,
        resource_identifier: Optional[str] = None,
        description: Optional[str] = None,
        old_values: Optional[Dict] = None,
        new_values: Optional[Dict] = None,
        success: bool = True,
        error_message: Optional[str] = None,
        extra_data: Optional[Dict] = None,
        company_id: Optional[int] = None,
    ) -> AuditLog:
        """
        Create an immutable audit log entry with hash chain integrity.

        CMMC Level 2 AU-3.3.8 Compliance:
        - Each entry includes a SHA-256 hash of its content
        - Hash chain links each entry to the previous one
        - Sequence numbers enable gap detection

        ``company_id`` tenant-tags the row so audit data can be retrieved per
        tenant. It defaults to the company resolved at construction time and is
        deliberately excluded from the integrity hash (see ``compute_audit_hash``).

        Serialization of the global hash chain (``sequence_number`` is globally
        unique, one chain for the whole table):
          1. A transaction-level Postgres advisory lock (``_acquire_chain_lock``)
             is taken BEFORE reading the chain tail, so only one writer at a time
             allocates+inserts. It auto-releases at txn end and is a no-op on
             SQLite.
          2. The tail-read → allocate → hash → INSERT is wrapped in a SAVEPOINT
             (``begin_nested``) with a bounded retry. A unique-``sequence_number``
             collision (the residual race, e.g. on SQLite where there is no
             advisory lock) rolls back ONLY the savepoint — leaving the caller's
             OUTER transaction usable — then re-reads the tail, re-allocates, and
             retries. If every attempt collides, it degrades to the existing
             best-effort contract (log + return ``None``) with the session left
             un-poisoned.

        This method never propagates an audit failure to the caller.
        """
        try:
            # Include correlation ID for request tracing
            correlation_id = get_correlation_id()

            # Get timestamp for the entry
            timestamp = datetime.utcnow()

            # Get user info
            user_id = self.user.id if self.user else None
            user_email = self.user.email if self.user else None
            user_name = getattr(self.user, 'full_name', None) if self.user else None
            success_str = "true" if success else "false"

            # Tenant tag for this row (per-call override falls back to the
            # company resolved at construction). Not part of the hash input.
            effective_company_id = company_id if company_id is not None else self.company_id

            # Serialize the global chain's allocate+insert against concurrent
            # writers on PostgreSQL. Auto-releases at txn end; no-op on SQLite.
            self._acquire_chain_lock()

            # Allocate + insert under a savepoint, retrying a residual unique
            # sequence_number collision. Re-read the tail on every attempt so the
            # chain stays contiguous and ``previous_hash`` tracks the live tail.
            for attempt in range(_MAX_SEQUENCE_RETRIES):
                # Get next sequence number and previous hash (for chain integrity)
                sequence_number, previous_hash = self._get_next_sequence_and_previous_hash()

                # Compute integrity hash
                integrity_hash = compute_audit_hash(
                    sequence_number=sequence_number,
                    timestamp=timestamp,
                    user_id=user_id,
                    user_email=user_email,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    resource_identifier=resource_identifier,
                    description=description,
                    old_values=old_values,
                    new_values=new_values,
                    ip_address=self._ip_address,
                    session_id=correlation_id,
                    success=success_str,
                    previous_hash=previous_hash,
                )

                log_entry = AuditLog(
                    sequence_number=sequence_number,
                    integrity_hash=integrity_hash,
                    previous_hash=previous_hash,
                    timestamp=timestamp,
                    user_id=user_id,
                    user_email=user_email,
                    user_name=user_name,
                    company_id=effective_company_id,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    resource_identifier=resource_identifier,
                    description=description,
                    old_values=old_values,
                    new_values=new_values,
                    ip_address=self._ip_address,
                    user_agent=self._user_agent,
                    session_id=correlation_id,
                    success=success_str,
                    error_message=error_message,
                    extra_data=extra_data,
                )

                # SAVEPOINT around the INSERT: a unique sequence_number collision
                # rolls back ONLY this savepoint, so the caller's outer transaction
                # stays usable (this is the fix for the old poisoning). The
                # ``db.add`` MUST live inside the savepoint — ``begin_nested``
                # autoflushes on open, so adding before it would emit the INSERT
                # (and any collision) outside this try/except.
                nested = self.db.begin_nested()
                try:
                    self.db.add(log_entry)
                    self.db.flush()  # Don't commit - let the caller handle transaction
                    return log_entry
                except IntegrityError:
                    # Residual sequence_number race: roll back ONLY the savepoint
                    # so the caller's outer txn stays usable, then re-read the tail
                    # and retry with a freshly built entry on the next iteration.
                    nested.rollback()
                    continue

            # Exhausted retries without inserting; fall through to best-effort.
            logger.error(
                "Failed to create audit log: sequence_number collision persisted "
                f"after {_MAX_SEQUENCE_RETRIES} attempts"
            )
            return None
        except Exception as e:
            logger.error(f"Failed to create audit log: {e}")
            # Don't raise - audit logging should not break the main operation
            return None

    def log_create(
        self,
        resource_type: str,
        resource_id: int,
        resource_identifier: str,
        new_values: Any = None,
        description: Optional[str] = None,
        extra_data: Optional[Dict] = None,
    ) -> AuditLog:
        """Log a CREATE action."""
        new_dict = self._model_to_dict(new_values) if new_values else None
        desc = description or f"Created {resource_type}: {resource_identifier}"

        return self.log(
            action=self.ACTIONS["CREATE"],
            resource_type=resource_type,
            resource_id=resource_id,
            resource_identifier=resource_identifier,
            description=desc,
            new_values=new_dict,
            extra_data=extra_data,
        )

    def log_update(
        self,
        resource_type: str,
        resource_id: int,
        resource_identifier: str,
        old_values: Any = None,
        new_values: Any = None,
        description: Optional[str] = None,
        extra_data: Optional[Dict] = None,
        action: str = None,
    ) -> AuditLog:
        """Log an UPDATE action with change tracking."""
        old_dict = self._model_to_dict(old_values) if old_values else {}
        new_dict = self._model_to_dict(new_values) if new_values else {}

        # Calculate changes
        changes = self._get_changes(old_dict, new_dict)

        if not changes and action != "restore":
            # No actual changes - skip logging (unless it's a restore)
            return None

        # Use custom action verb if provided
        action_verb = action.title() if action else "Updated"
        desc = description or f"{action_verb} {resource_type}: {resource_identifier}"

        return self.log(
            action=action.upper() if action else self.ACTIONS["UPDATE"],
            resource_type=resource_type,
            resource_id=resource_id,
            resource_identifier=resource_identifier,
            description=desc,
            old_values=old_dict,
            new_values=new_dict,
            extra_data={"changes": changes, **(extra_data or {})},
        )

    def log_delete(
        self,
        resource_type: str,
        resource_id: int,
        resource_identifier: str,
        old_values: Any = None,
        description: Optional[str] = None,
        extra_data: Optional[Dict] = None,
        soft_delete: bool = False,
    ) -> AuditLog:
        """Log a DELETE action (soft or hard delete)."""
        old_dict = self._model_to_dict(old_values) if old_values else None
        delete_type = "soft deleted" if soft_delete else "deleted"
        desc = description or f"{delete_type.title()} {resource_type}: {resource_identifier}"

        return self.log(
            action=self.ACTIONS["DELETE"],
            resource_type=resource_type,
            resource_id=resource_id,
            resource_identifier=resource_identifier,
            description=desc,
            old_values=old_dict,
            extra_data={"soft_delete": soft_delete, **(extra_data or {})},
        )

    def log_status_change(
        self,
        resource_type: str,
        resource_id: int,
        resource_identifier: str,
        old_status: str,
        new_status: str,
        description: Optional[str] = None,
        extra_data: Optional[Dict] = None,
    ) -> AuditLog:
        """Log a STATUS_CHANGE action."""
        desc = (
            description
            or f"Changed {resource_type} status: {resource_identifier} from '{old_status}' to '{new_status}'"
        )

        return self.log(
            action=self.ACTIONS["STATUS_CHANGE"],
            resource_type=resource_type,
            resource_id=resource_id,
            resource_identifier=resource_identifier,
            description=desc,
            old_values={"status": old_status},
            new_values={"status": new_status},
            extra_data=extra_data,
        )


def get_audit_service(db: Session, user: Optional[User] = None, request: Optional[Request] = None) -> AuditService:
    """Factory function to create AuditService instance."""
    return AuditService(db, user, request)
