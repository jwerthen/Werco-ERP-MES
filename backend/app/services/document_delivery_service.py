"""Explicit SMTP delivery of immutable, reviewed commercial documents.

SMTP has no reliable replay key or recipient-delivery receipt. Persist the claim
before transport and never retry sending/accepted/unknown attempts automatically.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage

import aiosmtplib
from fastapi import HTTPException
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.db.locks import acquire_generator_lock
from app.models.document_delivery import DocumentDelivery
from app.models.part import Part
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.quote import Quote, QuoteLine, QuoteStatus
from app.models.role_permission import DEFAULT_ROLE_PERMISSIONS, RolePermission
from app.models.user import User, UserRole
from app.schemas.document_delivery import DocumentDeliveryResponse
from app.services.document_pdf_service import build_purchase_order_document, build_quote_document, quote_pdf_context

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_DOCUMENT_LINES = 250


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()


def current_issue_date():
    return date.today()


def smtp_configured():
    return bool(settings.SMTP_USER and settings.SMTP_PASSWORD and settings.SMTP_HOST and settings.SMTP_FROM)


@dataclass(frozen=True)
class DeliveryMessage:
    recipient: str
    subject: str
    body: str
    provider_message_id: str
    attachment: bytes
    attachment_name: str


class DefiniteEmailFailure(Exception):
    """Known rejection before the server accepted this message."""


async def dispatch_document_email(delivery: DeliveryMessage):
    """No queue/autoretry: a DATA transport interruption has an unknown outcome."""
    if not smtp_configured():
        raise DefiniteEmailFailure('Email transport is not configured.')
    message = EmailMessage()
    message['From'] = f'{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM}>'
    message['To'] = delivery.recipient
    message['Subject'] = delivery.subject
    message['Message-ID'] = delivery.provider_message_id
    message.set_content(delivery.body)
    message.add_attachment(
        delivery.attachment, maintype='application', subtype='pdf', filename=delivery.attachment_name
    )
    potentially_submitted = False
    try:
        implicit_tls = settings.SMTP_PORT == 465
        # aiosmtplib otherwise opportunistically upgrades during connect(). Keep
        # exactly one TLS negotiation: implicit TLS on port 465, explicit STARTTLS on port 587.
        async with aiosmtplib.SMTP(
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            timeout=30,
            start_tls=False,
            use_tls=implicit_tls,
        ) as smtp:
            if not implicit_tls:
                await smtp.starttls()
            await smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            potentially_submitted = True
            errors, _ = await smtp.send_message(message)
            if errors:
                raise DefiniteEmailFailure('The mail server refused the recipient.')
    except (aiosmtplib.SMTPAuthenticationError, aiosmtplib.SMTPRecipientsRefused, aiosmtplib.SMTPSenderRefused) as exc:
        raise DefiniteEmailFailure('The mail server rejected authentication, sender or recipient.') from exc
    except aiosmtplib.SMTPDataError as exc:
        # The server explicitly rejected DATA; it did not accept responsibility.
        raise DefiniteEmailFailure('The mail server rejected the message content.') from exc
    except Exception as exc:
        if not potentially_submitted:
            raise DefiniteEmailFailure(
                'The mail connection failed before message submission. No email was sent.'
            ) from exc
        raise


class DocumentDeliveryService:
    def __init__(self, db, company_id, user, audit):
        self.db, self.company_id, self.user, self.audit = db, company_id, user, audit

    def can_send(self, entity_type):
        if getattr(self.user, '_read_only_company_context', False):
            return False
        if self.user.is_superuser or self.user.role == UserRole.PLATFORM_ADMIN:
            return True
        if entity_type == 'purchase_order':
            override = (
                self.db.query(RolePermission)
                .filter(RolePermission.company_id == self.company_id, RolePermission.role == self.user.role)
                .first()
            )
            permissions = override.permissions if override else DEFAULT_ROLE_PERMISSIONS.get(self.user.role, [])
            return 'purchasing:approve' in permissions and 'purchasing:view' in permissions
        return True

    def authorize(self, entity_type, write=False):
        roles = ['admin', 'manager'] if entity_type == 'purchase_order' else ['admin', 'manager', 'supervisor']
        role = getattr(self.user.role, 'value', self.user.role)
        if role not in roles and not self.user.is_superuser and self.user.role != UserRole.PLATFORM_ADMIN:
            raise HTTPException(403, 'Your role cannot send or view this document delivery.')
        if entity_type == 'purchase_order' and not self.user.is_superuser and self.user.role != UserRole.PLATFORM_ADMIN:
            override = (
                self.db.query(RolePermission)
                .filter(RolePermission.company_id == self.company_id, RolePermission.role == self.user.role)
                .first()
            )
            permissions = override.permissions if override else DEFAULT_ROLE_PERMISSIONS.get(self.user.role, [])
            if 'purchasing:view' not in permissions:
                raise HTTPException(403, 'Your role cannot view purchase-order deliveries.')
        if write and not self.can_send(entity_type):
            raise HTTPException(403, 'Your current role or read-only context cannot send this document.')

    def get(self, delivery_id, lock=False):
        query = self.db.query(DocumentDelivery).filter(
            DocumentDelivery.id == delivery_id, DocumentDelivery.company_id == self.company_id
        )
        record = (query.with_for_update() if lock else query).populate_existing().first()
        if not record:
            raise HTTPException(404, 'Document delivery not found')
        self.authorize(record.entity_type)
        return record

    def response(self, record, replayed=False):
        available = record.status == 'prepared' and smtp_configured() and self.can_send(record.entity_type)
        reason = (
            None
            if available
            else (
                'SMTP is not configured. Ask an administrator to configure email delivery.'
                if record.status == 'prepared'
                else 'This attempt is already recorded. Inspect its status; it will not be sent again.'
            )
        )
        if not self.can_send(record.entity_type):
            reason = 'Your current role or read-only company context cannot send this document.'
        return DocumentDeliveryResponse(
            id=record.id,
            entity_type=record.entity_type,
            entity_id=record.entity_id,
            document_number=record.document_number,
            issue_date=record.issue_date,
            recipient=record.recipient,
            subject=record.subject,
            body=record.body,
            attachment_name=record.attachment_name,
            attachment_sha256=record.attachment_sha256,
            attachment_size=record.attachment_size,
            status=record.status,
            status_detail=record.status_detail,
            version=record.version,
            provider_message_id=record.provider_message_id,
            created_at=record.created_at,
            attempted_at=record.attempted_at,
            accepted_at=record.accepted_at,
            send_available=available,
            unavailable_reason=reason,
            replayed=replayed,
            manually_verified=record.verified_at is not None,
            verified_at=record.verified_at,
            verification_note=record.verification_note,
        )

    def history(self, entity_type, entity_id):
        self.authorize(entity_type)
        records = (
            self.db.query(DocumentDelivery)
            .filter(
                DocumentDelivery.company_id == self.company_id,
                DocumentDelivery.entity_type == entity_type,
                DocumentDelivery.entity_id == entity_id,
            )
            .order_by(DocumentDelivery.id.desc())
            .limit(50)
            .all()
        )
        return [self.response(record) for record in records]

    def source(self, entity_type, entity_id, lock=False, issue_date_override=None):
        self.authorize(entity_type)
        if entity_type == 'quote':
            query = (
                self.db.query(Quote)
                .options(joinedload(Quote.lines).joinedload(QuoteLine.part))
                .filter(Quote.id == entity_id, Quote.company_id == self.company_id)
            )
            record = (query.with_for_update(of=Quote) if lock else query).populate_existing().first()
            if not record:
                raise HTTPException(404, 'Quote not found')
            if record.status not in [QuoteStatus.DRAFT, QuoteStatus.PENDING, QuoteStatus.SENT]:
                raise HTTPException(409, 'Only draft, pending or sent quotes can be emailed.')
            if not record.lines or len(record.lines) > MAX_DOCUMENT_LINES:
                raise HTTPException(422, 'Email documents require between 1 and 250 lines.')
            for line in record.lines:
                if line.company_id != self.company_id or (line.part and line.part.company_id != self.company_id):
                    raise HTTPException(404, 'Quote line not found')
            if lock:
                self.db.query(QuoteLine).filter(
                    QuoteLine.quote_id == record.id, QuoteLine.company_id == self.company_id
                ).order_by(QuoteLine.id).with_for_update().populate_existing().all()
                self.db.query(Part).filter(
                    Part.company_id == self.company_id,
                    Part.id.in_([line.part_id for line in record.lines if line.part_id]),
                ).order_by(Part.id).with_for_update().populate_existing().all()
                self.db.expire(record, ['lines'])
            context = quote_pdf_context(self.db, record, self.company_id, lock=lock)
            data = dict(
                header=self.columns(record),
                lines=[self.columns(line) for line in sorted(record.lines, key=lambda line: line.id)],
                part_names=[
                    (line.part.part_number, line.part.name) if line.part else None
                    for line in sorted(record.lines, key=lambda line: line.id)
                ],
                estimate=context,
            )
            return record, digest(data), record.customer_email or '', record.quote_number, None
        query = (
            self.db.query(PurchaseOrder)
            .options(
                joinedload(PurchaseOrder.vendor), joinedload(PurchaseOrder.lines).joinedload(PurchaseOrderLine.part)
            )
            .filter(
                PurchaseOrder.id == entity_id,
                PurchaseOrder.company_id == self.company_id,
                PurchaseOrder.is_deleted == False,
            )
        )
        record = (query.with_for_update(of=PurchaseOrder) if lock else query).populate_existing().first()
        if not record:
            raise HTTPException(404, 'Purchase order not found')
        if record.status not in [POStatus.DRAFT, POStatus.APPROVED, POStatus.SENT]:
            raise HTTPException(409, 'Only draft, approved or sent purchase orders can be emailed.')
        if (
            not record.vendor
            or record.vendor.company_id != self.company_id
            or record.vendor.is_deleted
            or not record.vendor.is_active
        ):
            raise HTTPException(409, 'The supplier is unavailable. Review this purchase order first.')
        if not record.lines or len(record.lines) > MAX_DOCUMENT_LINES:
            raise HTTPException(422, 'Email documents require between 1 and 250 lines.')
        for line in record.lines:
            if line.company_id != self.company_id or (line.part and line.part.company_id != self.company_id):
                raise HTTPException(404, 'Purchase order line not found')
        if lock:
            self.db.query(PurchaseOrderLine).filter(
                PurchaseOrderLine.purchase_order_id == record.id, PurchaseOrderLine.company_id == self.company_id
            ).order_by(PurchaseOrderLine.id).with_for_update().populate_existing().all()
            self.db.query(Vendor).filter(
                Vendor.id == record.vendor_id, Vendor.company_id == self.company_id
            ).with_for_update().populate_existing().first()
            self.db.query(Part).filter(
                Part.company_id == self.company_id, Part.id.in_([line.part_id for line in record.lines if line.part_id])
            ).order_by(Part.id).with_for_update().populate_existing().all()
            self.db.query(User).filter(
                User.id == record.created_by, User.company_id == self.company_id
            ).with_for_update().populate_existing().first()
            self.db.expire(record, ['lines', 'vendor'])
        if (
            record.created_by
            and not self.db.query(User.id)
            .filter(User.id == record.created_by, User.company_id == self.company_id)
            .first()
        ):
            raise HTTPException(404, 'Purchase order buyer not found')
        from app.api.endpoints.print_reports import get_purchase_order_print_data

        data = get_purchase_order_print_data(record.id, self.db, self.user, self.company_id).model_dump()
        data.pop('printed_at', None)
        data.pop('status', None)
        issue_date = record.order_date or issue_date_override or current_issue_date()
        data['order_date'] = issue_date.strftime('%m/%d/%Y')
        header = self.columns(record)
        header['order_date'] = issue_date
        fingerprint = digest(
            dict(
                print=data,
                header=header,
                lines=[self.columns(line) for line in sorted(record.lines, key=lambda line: line.id)],
            )
        )
        data['_issue_date'] = issue_date
        return record, fingerprint, record.vendor.email or '', record.po_number, data

    @staticmethod
    def columns(record):
        # Sending status/timestamps are workflow metadata, not PDF content. Their
        # transition after acceptance must not turn an identical PDF into a new send.
        ignored = {'status', 'created_at', 'updated_at', 'version', 'request_key', 'request_hash'}
        return {
            column.name: getattr(record, column.name)
            for column in record.__table__.columns
            if column.name not in ignored
        }

    def preview(self, entity_type, entity_id, new_attempt=False):
        self.authorize(entity_type, write=True)
        acquire_generator_lock(self.db, 'document_delivery', self.company_id)
        source, source_hash, recipient, number, print_data = self.source(entity_type, entity_id, lock=True)
        existing = (
            self.db.query(DocumentDelivery)
            .filter(
                DocumentDelivery.company_id == self.company_id,
                DocumentDelivery.entity_type == entity_type,
                DocumentDelivery.entity_id == entity_id,
            )
            .order_by(DocumentDelivery.id.desc())
            .all()
        )
        # An unresolved attempt can have reached SMTP. Preparing another snapshot
        # must not bypass that uncertainty and accidentally resend the same order.
        unresolved = next((row for row in existing if row.status in ['sending', 'unknown']), None)
        same = next((row for row in existing if row.source_hash == source_hash), None)
        reusable = same and (same.status == 'prepared' or (same.status == 'accepted' and not new_attempt))
        if unresolved or reusable:
            return self.response(unresolved or same, replayed=True)
        if entity_type == 'quote':
            pdf = build_quote_document(self.db, source, self.company_id)
        else:
            pdf = build_purchase_order_document({**print_data, 'printed_at': current_issue_date().strftime('%m/%d/%Y')})
        if len(pdf) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(422, 'The PDF exceeds the 5 MB email attachment limit.')
        row = DocumentDelivery(
            company_id=self.company_id,
            entity_type=entity_type,
            entity_id=entity_id,
            document_number=number,
            issue_date=print_data["_issue_date"] if print_data else None,
            source_hash=source_hash,
            attachment_name=f'{number}.pdf',
            attachment_sha256=hashlib.sha256(pdf).hexdigest(),
            attachment=pdf,
            attachment_size=len(pdf),
            recipient=recipient,
            subject=f'{"Quote" if entity_type == "quote" else "Purchase order"} {number}',
            body=f'Please find {"quote" if entity_type == "quote" else "purchase order"} {number} attached.\n\nPlease contact us with any questions.',
            status='prepared',
            created_by=self.user.id,
        )
        self.db.add(row)
        self.db.flush()
        self.audit.log_create(
            'document_delivery',
            row.id,
            number,
            new_values={
                'status': 'prepared',
                'attachment_sha256': row.attachment_sha256,
                'source_id': entity_id,
                'source_type': entity_type,
            },
        )
        return self.response(row)

    async def send(self, delivery_id, payload):
        acquire_generator_lock(self.db, 'document_delivery', self.company_id)
        record = self.get(delivery_id, lock=True)
        self.authorize(record.entity_type, write=True)
        request_hash = digest(payload.model_dump(mode='json', exclude={'request_key', 'expected_version'}))
        prior = (
            self.db.query(DocumentDelivery)
            .filter(DocumentDelivery.company_id == self.company_id, DocumentDelivery.request_key == payload.request_key)
            .first()
        )
        if record.status != 'prepared' or prior:
            if prior and prior.id == record.id and record.request_hash == request_hash:
                return self.response(record, replayed=True)
            raise HTTPException(
                409, 'This delivery or retry key has already been used. Refresh delivery status before sending.'
            )
        unresolved = (
            self.db.query(DocumentDelivery.id)
            .filter(
                DocumentDelivery.company_id == self.company_id,
                DocumentDelivery.entity_type == record.entity_type,
                DocumentDelivery.entity_id == record.entity_id,
                DocumentDelivery.id != record.id,
                DocumentDelivery.status.in_(['sending', 'unknown']),
            )
            .first()
        )
        if unresolved:
            raise HTTPException(
                409,
                'Another delivery for this document has an unresolved SMTP outcome. Check its status before sending again.',
            )
        if record.version != payload.expected_version:
            raise HTTPException(
                409, 'This email review changed. Reload it before sending edited recipient or message fields.'
            )
        if not smtp_configured():
            raise HTTPException(503, 'Email transport is not configured. No message was sent.')
        source, source_hash, _, _, _ = self.source(record.entity_type, record.entity_id, lock=True)
        if source_hash != record.source_hash:
            raise HTTPException(
                409, 'The document changed after this PDF was prepared. Create a fresh preview before sending.'
            )
        record.recipient = str(payload.recipient)
        record.subject, record.body = payload.subject, payload.body
        record.request_key, record.request_hash = payload.request_key, request_hash
        record.provider_message_id = f'<werco-{uuid.uuid4()}@{settings.SMTP_FROM.rsplit("@", 1)[-1]}>'
        record.status, record.status_detail = 'sending', 'Sending to SMTP; acceptance is not yet known. Do not resend.'
        record.version += 1
        record.sent_by, record.attempted_at = self.user.id, datetime.utcnow()
        self.audit.log_create(
            'document_delivery_attempt',
            record.id,
            record.document_number,
            new_values={
                'recipient': record.recipient,
                'subject': record.subject,
                'attachment_sha256': record.attachment_sha256,
                'status': 'sending',
            },
        )
        # Materialize the immutable transport payload while this transaction is
        # open. ORM attributes expire on commit, and the PDF is deferred; reading
        # either during SMTP would open a new DB transaction across network I/O.
        delivery_id, claim_version = record.id, record.version
        message = DeliveryMessage(
            recipient=record.recipient,
            subject=record.subject,
            body=record.body,
            provider_message_id=record.provider_message_id,
            attachment=bytes(record.attachment),
            attachment_name=record.attachment_name,
        )
        self.db.commit()  # Durable claim before irreversible SMTP side effect.
        try:
            await dispatch_document_email(message)
            outcome, detail = 'accepted', 'SMTP accepted this message. Recipient delivery has not been confirmed.'
        except DefiniteEmailFailure as exc:
            outcome, detail = 'failed', str(exc)[:500]
        except Exception:
            # Never claim failure after an ambiguous DATA/network interruption.
            outcome, detail = (
                'unknown',
                'SMTP acceptance could not be confirmed. Check the mail server before taking further action; this attempt will not resend.',
            )
        # Persist the transport outcome even if the actor's permissions changed
        # while SMTP was in flight; authorization was checked before dispatch.
        record = (
            self.db.query(DocumentDelivery)
            .filter(DocumentDelivery.id == delivery_id, DocumentDelivery.company_id == self.company_id)
            .with_for_update()
            .populate_existing()
            .one()
        )
        if record.status != 'sending' or record.version != claim_version:
            previous_status = record.status
            conflict = (
                outcome in ['accepted', 'failed']
                and previous_status in ['accepted', 'failed']
                and outcome != previous_status
            )
            if conflict:
                record.status = 'unknown'
                record.status_detail = f'Conflict: late SMTP reported {outcome}, while manual verification recorded {previous_status}. Check the mail server again before another attempt. The verification note is retained.'
            else:
                record.status_detail = (record.status_detail or '')[
                    :350
                ] + f' Late SMTP outcome: {outcome}; recorded verification was retained.'
            record.version += 1
            self.audit.log_status_change(
                'document_delivery',
                record.id,
                record.document_number,
                old_status=previous_status,
                new_status=record.status,
                extra_data={
                    'late_transport_outcome': outcome,
                    'verification_conflict': conflict,
                    'claim_version': claim_version,
                    'verification_note': record.verification_note,
                },
            )
            self.db.commit()
            return self.response(record)
        record.status, record.status_detail = outcome, detail
        record.version += 1
        if outcome == 'accepted':
            record.accepted_at = datetime.utcnow()
            self.update_accepted_source(record, source_hash)
        self.audit.log_status_change(
            'document_delivery',
            record.id,
            record.document_number,
            old_status='sending',
            new_status=outcome,
            extra_data={
                'attachment_sha256': record.attachment_sha256,
                'provider_message_id': record.provider_message_id,
            },
        )
        self.db.commit()
        return self.response(record)

    def update_accepted_source(self, record, source_hash):
        try:
            current, current_hash, _, _, _ = self.source(
                record.entity_type, record.entity_id, lock=True, issue_date_override=record.issue_date
            )
            if current_hash == source_hash:
                old_status = current.status.value
                current.status = QuoteStatus.SENT if record.entity_type == 'quote' else POStatus.SENT
                if record.entity_type == 'purchase_order' and not current.order_date:
                    current.order_date = record.issue_date
                self.audit.log_status_change(
                    record.entity_type,
                    current.id,
                    record.document_number,
                    old_status=old_status,
                    new_status='sent',
                    extra_data={
                        'delivery_id': record.id,
                        'delivery_status': 'accepted',
                        'manually_verified': record.verified_at is not None,
                    },
                )
            else:
                record.status_detail += ' The source changed; its workflow status was retained.'
        except HTTPException:
            record.status_detail += ' The source changed; its workflow status was retained.'

    def reconcile(self, delivery_id, payload):
        acquire_generator_lock(self.db, 'document_delivery', self.company_id)
        record = self.get(delivery_id, lock=True)
        self.authorize(record.entity_type, write=True)
        if (
            self.user.role not in [UserRole.ADMIN, UserRole.MANAGER, UserRole.PLATFORM_ADMIN]
            and not self.user.is_superuser
        ):
            raise HTTPException(403, 'Only an administrator or manager can verify an unresolved email outcome.')
        if record.version != payload.expected_version:
            raise HTTPException(409, 'This delivery status changed. Reload it before recording verification.')
        now = datetime.utcnow()
        if record.status != 'unknown' and not (
            record.status == 'sending' and record.attempted_at and now - record.attempted_at >= timedelta(minutes=10)
        ):
            raise HTTPException(
                409,
                'Only unknown outcomes or sends pending for at least 10 minutes can be verified. Active sends must finish first.',
            )
        previous_status = record.status
        record.status = payload.outcome
        record.version += 1
        record.verified_at, record.verified_by = now, self.user.id
        record.verification_note = payload.verification_note
        record.status_detail = (
            'Manually verified as sent after checking the mail server. This is operator verification; recipient delivery is not confirmed.'
            if payload.outcome == 'accepted'
            else 'Manually verified as not sent after checking the mail server. A new reviewed attempt can now be prepared.'
        )
        if payload.outcome == 'accepted':
            self.update_accepted_source(record, record.source_hash)
        self.audit.log_status_change(
            'document_delivery',
            record.id,
            record.document_number,
            old_status=previous_status,
            new_status=payload.outcome,
            extra_data={
                'manually_verified': True,
                'verification_note': payload.verification_note,
                'verified_by': self.user.id,
                'attachment_sha256': record.attachment_sha256,
            },
        )
        return self.response(record)
