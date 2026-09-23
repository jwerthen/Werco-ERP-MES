"""Small, owner-authorized views of saved PDF extraction for Hank chat.

PDF bytes are analyzed once by intake. Chat receives a manifest and fetches
bounded evidence pages only when needed; it never re-uploads the PDF to Claude.
"""

from fastapi import HTTPException

from app.schemas.hank_intake import IntakeExtraction
from app.services.hank_intake_service import HankIntakeService

READY_STATUSES = frozenset({'awaiting_review', 'planned', 'completed'})


def _source(db, company_id, user, file_id):
    if type(file_id) is not int or file_id <= 0:
        raise HTTPException(422, 'Choose a saved PDF from document intake.')
    row = HankIntakeService(db, user, company_id).file(file_id)
    if row.status not in READY_STATUSES or not row.analysis_json:
        raise HTTPException(409, 'Wait for this PDF to finish analysis before using it in chat.')
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
    return {
        'data': {
            'file_id': row.id,
            'version': row.version,
            'filename': row.filename,
            'section': section,
            'items': [
                {'index': index, **item.model_dump(mode='json')} for index, item in enumerate(items[offset:end], offset)
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
        return {'data': {'error': 'Choose a saved PDF and a valid purchase order.'}, 'is_error': True}
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
        'summary': 'matched PDF delivery evidence to receiving records for review',
        'references': [_reference(row)],
    }
