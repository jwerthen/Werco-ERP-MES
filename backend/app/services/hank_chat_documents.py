"""Small, owner-authorized views of saved document extraction for Hank chat.

Source bytes are analyzed once by intake. Chat receives a manifest and fetches
bounded evidence only when needed; it never re-uploads the source to Claude.
"""

from fastapi import HTTPException

from app.schemas.hank_intake import IntakeExtraction
from app.services.hank_intake_service import HankIntakeService

READY_STATUSES = frozenset({'awaiting_review', 'planned', 'completed'})


def _source(db, company_id, user, file_id):
    if type(file_id) is not int or file_id <= 0:
        raise HTTPException(422, 'Choose a saved document from document intake.')
    row = HankIntakeService(db, user, company_id).file(file_id)
    if row.status not in READY_STATUSES or not row.analysis_json:
        raise HTTPException(409, 'Wait for this document to finish analysis before using it in chat.')
    return row, IntakeExtraction.model_validate(row.analysis_json)


def _reference(row):
    return {
        'type': 'hank_intake',
        'id': row.id,
        'label': row.filename,
        'url': f'/?hank_work=intake&hank_id={row.id}',
    }


def attachment_manifest(db, company_id, user, file_ids):
    """Authorize every attachment before starting the response or making AI calls."""
    manifest, references = [], []
    for file_id in dict.fromkeys(file_ids):
        row, analysis = _source(db, company_id, user, file_id)
        manifest.append(
            {
                'file_id': row.id,
                'version': row.version,
                'filename': row.filename,
                'source_format': analysis.source_format,
                'classification': analysis.classification,
                'confidence': analysis.confidence,
                'summary': analysis.summary,
                'field_count': len(analysis.fields),
                'line_count': len(analysis.lines),
                'warnings': analysis.warnings,
            }
        )
        references.append(_reference(row))
    return manifest, references


def document_evidence(*, db, company_id, user, file_id, section='lines', offset=0, limit=5):
    """Return complete JSON evidence slices instead of cutting a large tool result."""
    if section not in ('fields', 'lines') or type(offset) is not int or not 0 <= offset <= 50:
        return {'data': {'error': 'Choose fields or lines and an offset between 0 and 50.'}, 'is_error': True}
    if type(limit) is not int or not 1 <= limit <= 5:
        return {'data': {'error': 'Read between 1 and 5 evidence items at a time.'}, 'is_error': True}
    try:
        row, analysis = _source(db, company_id, user, file_id)
    except HTTPException as exc:
        return {'data': {'error': exc.detail}, 'is_error': True}
    items = getattr(analysis, section)
    end = min(offset + limit, len(items))
    source_units = sorted({proof.page for item in items[offset:end] for proof in item.evidence})
    return {
        'data': {
            'file_id': row.id,
            'version': row.version,
            'filename': row.filename,
            'source_format': analysis.source_format,
            'source_labels': {
                index: analysis.source_labels[index - 1]
                for index in source_units
                if index <= len(analysis.source_labels)
            },
            'section': section,
            'items': [
                {'index': index, **item.model_dump(mode='json', exclude_none=True)}
                for index, item in enumerate(items[offset:end], offset)
            ],
            'total': len(items),
            'next_offset': end if end < len(items) else None,
            'requires_review': True,
        },
        'summary': (
            f'read {section} {offset + 1}–{end} from {row.filename}'
            if end > offset
            else f'no more {section} in {row.filename}'
        ),
        'references': [_reference(row)],
    }


def receiving_document(*, db, company_id, user, file_id, purchase_order_id=None, offset=0, limit=5):
    from app.services.hank_intake_receiving_service import HankIntakeReceivingService

    if (
        type(file_id) is not int
        or file_id <= 0
        or (purchase_order_id is not None and (type(purchase_order_id) is not int or purchase_order_id <= 0))
    ):
        return {'data': {'error': 'Choose a saved document and a valid purchase order.'}, 'is_error': True}
    if type(offset) is not int or not 0 <= offset <= 50 or type(limit) is not int or not 1 <= limit <= 5:
        return {'data': {'error': 'Read between 1 and 5 lines with an offset between 0 and 50.'}, 'is_error': True}
    try:
        draft = HankIntakeReceivingService(db, user, company_id).draft(file_id, purchase_order_id)
        row, _ = _source(db, company_id, user, file_id)
    except HTTPException as exc:
        return {'data': {'error': exc.detail}, 'is_error': True}
    data = draft.model_dump(mode='json')
    count = len(data['lines'])
    data['lines'] = data['lines'][offset : offset + limit]
    data['line_count'] = count
    data['next_offset'] = offset + limit if offset + limit < count else None
    # Many ambiguous candidates are actionable in the receiving picker; do not
    # spend the conversation budget serializing all of them on every lookup.
    data['purchase_order_count'] = len(data['purchase_orders'])
    data['purchase_orders'] = data['purchase_orders'][:10]
    for line in data['lines']:
        line['candidate_count'] = len(line['candidates'])
        line['candidates'] = line['candidates'][:5]
    data['source_intake_file_id'] = row.id
    data['source_intake_version'] = row.version
    return {
        'data': data,
        'summary': 'matched document delivery evidence to receiving records for review',
        'references': [_reference(row)],
    }


def purchase_order_document(*, db, company_id, user, file_id, offset=0, limit=5):
    """Resolve saved PO evidence without another model call or creating records."""
    from app.services.hank_intake_purchase_order_service import HankIntakePurchaseOrderService

    if type(file_id) is not int or file_id <= 0:
        return {'data': {'error': 'Choose a saved purchase-order document.'}, 'is_error': True}
    if type(offset) is not int or not 0 <= offset <= 50 or type(limit) is not int or not 1 <= limit <= 5:
        return {'data': {'error': 'Read between 1 and 5 lines with an offset between 0 and 50.'}, 'is_error': True}
    try:
        draft = HankIntakePurchaseOrderService(db, user, company_id).draft(file_id)
        row, _ = _source(db, company_id, user, file_id)
    except HTTPException as exc:
        return {'data': {'error': exc.detail}, 'is_error': True}
    data = draft.model_dump(mode='json')
    count = len(data['lines'])
    data['lines'] = data['lines'][offset : offset + limit]
    data['line_count'] = count
    data['next_offset'] = offset + limit if offset + limit < count else None
    data['vendor_count'] = len(data['vendors'])
    data['vendors'] = data['vendors'][:10]
    for line in data['lines']:
        line['candidate_count'] = len(line['candidates'])
        line['candidates'] = line['candidates'][:5]
    data['source_intake_file_id'] = row.id
    data['source_intake_version'] = row.version
    data['requires_review'] = True
    return {
        'data': data,
        'summary': 'matched purchase-order document to current vendors and parts for review',
        'references': [_reference(row)],
    }
