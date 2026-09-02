"""
Comprehensive Audit Service for AS9100D and CMMC Level 2 Compliance.
Provides centralized audit logging for all entity changes with tamper detection.

CMMC Level 2 Control: AU-3.3.8 - Protect Audit Information
- Immutable audit logs (DB triggers, migrations 008/060 -- always in force)
- Hash chain integrity: sequence numbers for gap detection + SHA-256 cryptographic
  hashing. ON BY DEFAULT but PAUSABLE at runtime via settings.AUDIT_HASH_CHAIN_ENABLED
  (see ``log()`` and ``PAUSED_CHAIN_PLACEHOLDER`` below). Paused, rows keep their full
  audit content and DB-level immutability but carry no hash and no chain link, gaps in
  sequence_number are normal, and they can never be verified retroactively. The
  immutability triggers are independent of that setting.
  See docs/AUDIT_LOG_RETENTION_RUNBOOK.md -> Pausing the hash chain.
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

from app.core.config import settings
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

# Placeholder written into integrity_hash while the chain is paused
# (settings.AUDIT_HASH_CHAIN_ENABLED = False). Reuses the 'LEGACY_' prefix that
# migration 008 established for pre-chain rows, because every consumer already
# tests exactly that prefix and skips the row: audit_integrity_service (3 Python
# sites + one SQL LIKE) and the /audit/integrity/record endpoint's `is_legacy`.
# The suffix makes a paused row distinguishable from a genuine 008 backfill.
# integrity_hash is String(64) NOT NULL with no unique constraint, so a repeated
# constant is fine.
PAUSED_CHAIN_PLACEHOLDER = "LEGACY_CHAIN_PAUSED"

# Postgres sequence backing sequence_number while the chain is paused. Created by
# migration 077, started safely past the current MAX. nextval() takes no
# transaction-scoped lock, which is the entire point: it is the only allocator
# that actually removes the global funnel rather than relocating it to the unique
# index. Gaps are expected and normal — nextval does not roll back with the
# caller's transaction.
_AUDIT_SEQUENCE_NAME = "audit_logs_sequence_number_seq"

# The two statements are written as plain literals rather than f-strings over
# _AUDIT_SEQUENCE_NAME on purpose: an f-string here trips bandit B608
# (hardcoded_sql_expressions), which is a blocking CI gate. It is a false positive —
# the only interpolated value would be a module constant, never user input — but a
# literal is clearer than a `# nosec` and costs nothing. Tests assert that
# _AUDIT_SEQUENCE_NAME actually appears in both, so the name cannot drift silently.
_NEXTVAL_SQL = "SELECT nextval('audit_logs_sequence_number_seq')"
_RESYNC_SEQUENCE_SQL = (
    "SELECT setval('audit_logs_sequence_number_seq', GREATEST("
    "(SELECT COALESCE(MAX(sequence_number), 0) FROM audit_logs), "
    "(SELECT last_value FROM audit_logs_sequence_number_seq)"
    "), true)"
)


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
        "visitor_log": "visitor_log",
        "signin_station": "signin_station",
        "authentication": "authentication",
        "system": "system",
    }

    # ``extra_data`` key under which every row written on a request that was
    # authenticated by an API TOKEN carries the credential marker
    # ``{"kind": "api_token", "api_token_id", "jti_prefix", "label"}``. A token's
    # writes are attributed to the bound user (correct -- it acts AS them) and
    # would otherwise be indistinguishable from that person's own interactive
    # actions, and from each other; the marker answers "which credential did
    # this" on every row, through this service, never by a direct write.
    CREDENTIAL_KEY = "credential"

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
        # Set only when ``get_current_user`` resolved the actor from an API token
        # (``user._api_token_id``); None for every interactive / system actor.
        self._credential = self._resolve_credential(user)

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

    @staticmethod
    def _resolve_credential(user: Optional[User]) -> Optional[Dict[str, Any]]:
        """The API-token marker for this actor, or None for an interactive / system actor."""
        token_id = getattr(user, "_api_token_id", None) if user is not None else None
        if token_id is None:
            return None
        return {
            "kind": "api_token",
            "api_token_id": token_id,
            "jti_prefix": getattr(user, "_api_token_jti_prefix", None),
            "label": getattr(user, "_api_token_label", None),
        }

    def _with_credential(self, extra_data: Optional[Dict]) -> Optional[Dict]:
        """Fold the credential marker into a row's ``extra_data`` (the marker always wins the key)."""
        if self._credential is None:
            return extra_data
        return {**(extra_data or {}), self.CREDENTIAL_KEY: dict(self._credential)}

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

    def _next_sequence_paused(self) -> int:
        """Allocate ``sequence_number`` while the hash chain is paused.

        PostgreSQL: ``nextval`` on the dedicated sequence. Lock-free, which is the
        whole reason the paused mode exists — allocating with the old MAX+1 tail
        read but no advisory lock would NOT remove the serialization, it would just
        move it onto the unique index and, after exhausting the retry budget, drop
        the audit row entirely. A state change with no audit row is worse than a
        state change with no hash.

        Other dialects (SQLite in tests): fall back to MAX+1. Correct there because
        the test backend is effectively single-writer, and the savepoint/retry
        wrapper in ``log()`` still covers a residual collision.
        """
        bind = self.db.get_bind()
        dialect = bind.dialect.name if bind is not None else ""
        if dialect == "postgresql":
            # SAVEPOINT around nextval, and this is not defensive padding. If the
            # sequence is absent — migration 077 not applied yet, 077 downgraded
            # while paused, or a Postgres DB bootstrapped by create_all which never
            # runs migrations — nextval raises UndefinedTable and PostgreSQL aborts
            # the ENTIRE transaction (SQLSTATE 25P02). log()'s broad except would
            # swallow the error, but every later statement in the CALLER's
            # transaction then fails with InFailedSqlTransaction, so the business
            # write dies too. That would break this class's core contract that an
            # audit failure never propagates to the caller, and would turn a
            # migration-ordering slip into an outage of every audited endpoint.
            #
            # A savepoint contains the abort, and we then degrade to the MAX+1
            # allocator: slower and not lock-free, but correct. Losing the
            # performance win is vastly better than losing the write.
            nested = self.db.begin_nested()
            try:
                next_sequence = int(self.db.execute(text(_NEXTVAL_SQL)).scalar_one())
                nested.commit()
                return next_sequence
            except Exception as exc:
                nested.rollback()
                logger.error(
                    f"Audit sequence '{_AUDIT_SEQUENCE_NAME}' unavailable while the hash chain is paused "
                    f"({exc}); falling back to MAX+1 allocation. Apply migration 077 — until then audit "
                    f"writes still work but the global serialization this mode removes is back."
                )
        next_sequence, _ = self._get_next_sequence_and_previous_hash()
        return next_sequence

    def _resync_paused_sequence(self) -> None:
        """Advance the paused-mode sequence past the table's current MAX.

        Needed because the two allocators can leapfrog each other. Concretely:
        pause (sequence hands out 6001..6050), resume (the chain allocates MAX+1 =
        6051..6070 and never touches the sequence, which is still at 6050), then
        pause again — ``nextval`` now returns 6051, which the resumed chain already
        used. Every retry collides, and after the retry budget ``log()`` DROPS the
        audit row. A state change with no audit row is the worst outcome available
        here, so this is a correctness fix, not a tidiness one.

        Called only from the collision-retry path, so the steady state pays nothing:
        the extra MAX read happens once after a mode flip, not on every write. It is
        also idempotent and safe to call concurrently — setval to a GREATEST() can
        only move the counter forward.
        """
        bind = self.db.get_bind()
        dialect = bind.dialect.name if bind is not None else ""
        if dialect != "postgresql":
            return
        # Savepoint for the same reason as _next_sequence_paused: a missing
        # sequence here would abort the caller's transaction, and this runs on an
        # error path where the caller is already mid-write.
        nested = self.db.begin_nested()
        try:
            self.db.execute(text(_RESYNC_SEQUENCE_SQL))
            nested.commit()
        except Exception as exc:
            nested.rollback()
            logger.error(f"Could not resync audit sequence '{_AUDIT_SEQUENCE_NAME}': {exc}")

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
            # Credential marker (API-token requests only) -- folded in here so
            # every helper (log_create / log_update / log_delete /
            # log_status_change / log) carries it.
            extra_data = self._with_credential(extra_data)

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

            chain_enabled = settings.AUDIT_HASH_CHAIN_ENABLED

            # Serialize the global chain's allocate+insert against concurrent
            # writers on PostgreSQL. Auto-releases at txn end; no-op on SQLite.
            # Skipped entirely while paused — this lock IS the cost being removed.
            if chain_enabled:
                self._acquire_chain_lock()

            # Allocate + insert under a savepoint, retrying a residual unique
            # sequence_number collision. Re-read the tail on every attempt so the
            # chain stays contiguous and ``previous_hash`` tracks the live tail.
            # The savepoint wrapper is kept in BOTH modes: it is the caller's
            # session-poisoning guard, not merely a collision guard.
            for attempt in range(_MAX_SEQUENCE_RETRIES):
                if chain_enabled:
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
                else:
                    # Paused: lock-free allocation, no tail read, no hash. The row
                    # is otherwise identical — same table, same audit content.
                    sequence_number = self._next_sequence_paused()
                    previous_hash = None
                    integrity_hash = PAUSED_CHAIN_PLACEHOLDER

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
                    if not chain_enabled:
                        # Paused mode: a collision here almost certainly means the
                        # sequence has fallen behind the table (the chain ran in
                        # enabled mode since the last pause and allocated MAX+1 past
                        # the sequence's last_value). Jump it forward, otherwise
                        # every remaining retry collides too and the row is lost.
                        self._resync_paused_sequence()
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
