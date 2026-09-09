"""Buyer counts, real PDF text/vector output and bounded pure rendering."""

import copy
from datetime import datetime, timezone
from io import BytesIO

import pytest
from fastapi import HTTPException
from pypdf import PdfReader

from app.schemas.nesting_buyer_pdf import BuyerPdfReport
from app.services.nesting_buyer_pdf import build_buyer_pdf, parse_report


def rectangle(x=0, y=0, width=12, height=8):
    return {
        'type': 'poly',
        'points': [
            {'x': x, 'y': y},
            {'x': x + width, 'y': y},
            {'x': x + width, 'y': y + height},
            {'x': x, 'y': y + height},
        ],
    }


def report():
    return {
        'version': 1,
        'units': 'in',
        'expectedCompanyId': 1,
        'projectName': 'Synthetic nesting project',
        'notes': 'Confirm specification before ordering.',
        'inputSha256': 'a' * 64,
        'solverVersion': 'werco-contour-v7',
        'groups': [
            {
                'id': 'steel',
                'name': 'A36',
                'material': 'Carbon steel',
                'materialDescription': 'A36 - planner supplied',
                'thicknessIn': 0.125,
                'selectionKind': 'full_sheet',
                'partRequirements': [
                    {'id': 'p', 'label': 'P001', 'name': 'Bracket <REF> & café', 'revision': 'A', 'quantity': 2}
                ],
                'baselinePurchaseSheets': [],
                'sheets': [
                    {
                        'number': 1,
                        'source': 'purchase',
                        'sourceLabel': '8 x 12 full sheet',
                        'widthIn': 8,
                        'lengthIn': 12,
                        'marginIn': 0.375,
                        'gapIn': 0.125,
                        'outer': rectangle(),
                        'holes': [],
                        'exclusions': [],
                        'placements': [
                            {'partId': 'p', 'originalInstance': i, 'loops': [rectangle(1 + 4 * i, 1, 2, 2)]}
                            for i in range(2)
                        ],
                    }
                ],
            }
        ],
    }


def pdf(value):
    return build_buyer_pdf(
        BuyerPdfReport.model_validate(value),
        company_name='Authenticated Company',
        company_id=1,
        prepared_by='Authenticated Estimator',
        user_id=7,
        generated_at=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )


def read(value):
    return PdfReader(BytesIO(value))


@pytest.mark.parametrize(
    'change', ['missing', 'duplicate', 'unknown', 'extra_instance', 'extra_part', 'number', 'fallback', 'two_pieces']
)
def test_refuses_inconsistent_original_accounting(change):
    value = report()
    group = value['groups'][0]
    sheet = group['sheets'][0]
    if change == 'missing':
        sheet['placements'].pop()
    elif change == 'duplicate':
        sheet['placements'][1]['originalInstance'] = 0
    elif change == 'unknown':
        sheet['placements'][0]['partId'] = 'foreign'
    elif change == 'extra_instance':
        sheet['placements'][1]['originalInstance'] = 2
    elif change == 'extra_part':
        group['partRequirements'].append({**group['partRequirements'][0], 'id': 'missing', 'label': 'P2'})
    elif change == 'number':
        sheet['number'] = 2
    elif change == 'fallback':
        group['selectionKind'] = 'recorded_piece'
    else:
        group['selectionKind'] = 'recorded_piece'
        group['baselinePurchaseSheets'] = [{'widthIn': 8, 'lengthIn': 12, 'quantity': 2}]
        sheet['source'] = 'recorded_piece'
        other = copy.deepcopy(sheet)
        other['number'] = 2
        other['placements'] = [sheet['placements'].pop()]
        group['sheets'].append(other)
    with pytest.raises(ValueError):
        BuyerPdfReport.model_validate(value)


@pytest.mark.parametrize('value', [b'{"version":1,"version":1}', b'[]', b'\xff', b'{"version": NaN}'])
def test_invalid_json_is_safe_422(value):
    with pytest.raises(HTTPException) as caught:
        parse_report(value)
    assert caught.value.status_code == 422


def test_pdf_preserves_text_unicode_and_exact_count_and_has_vector_paths():
    value = report()
    value['projectName'] = 'R&D <TYP> 😀 Ж'
    before = copy.deepcopy(value)
    result = pdf(value)
    reader = read(result)
    assert len(reader.pages) == 2
    text = '\n'.join(page.extract_text() for page in reader.pages)
    assert 'R&D <TYP> [U+1F600] [U+0416]' in text
    assert 'Bracket <REF> & café' in text and 'Font notice' in text
    assert 'Authenticated Company' in text and 'Authenticated Estimator' in text
    assert '2026-09-09 07:00:00 AM CDT' in text and 'Width x length (in)' in text and '8 x 12' in text
    assert 'P001' in text and 'not a purchase order' in text
    assert ' f*' in reader.pages[1].get_contents().get_data().decode('latin1') or 'B*' in reader.pages[
        1
    ].get_contents().get_data().decode('latin1')
    assert value == before
    assert pdf(value) == result


def test_conditional_piece_and_fallback_are_separate_from_purchase_summary():
    value = report()
    group = value['groups'][0]
    group['selectionKind'] = 'recorded_piece'
    group['baselinePurchaseSheets'] = [{'widthIn': 8, 'lengthIn': 12, 'quantity': 2}]
    sheet = group['sheets'][0]
    sheet['source'] = 'recorded_piece'
    sheet['sourceLabel'] = 'Recorded source #7 / observation 2'
    sheet['holes'] = [{'type': 'circle', 'cx': 10, 'cy': 6, 'r': 0.5}]
    sheet['exclusions'] = [{'outline': rectangle(10, 1, 1, 1), 'clearanceIn': 0.25}]
    text = ' '.join(' '.join(page.extract_text() for page in read(pdf(value)).pages).split())
    assert 'No full sheets in this conditional selection' in text
    assert 'full-sheet fallback' in text and 'Recorded source #7 / observation 2' in text
    assert 'not reserved or consumed' in text and '0.25' in text
    assert 'REPLACES the selected purchase quantities for G1' in text
    assert 'Do not add these fallback sheets' in text


def test_one_hundred_unique_parts_paginate_without_losing_tail_labels(tmp_path):
    value = report()
    group = value['groups'][0]
    group['partRequirements'] = [
        {
            'id': f'p{i}',
            'label': f'P{i:03}',
            'name': f'Original drawing {i:03} ' + ('long description ' * 5),
            'revision': 'REV-A',
            'quantity': 1,
        }
        for i in range(100)
    ]
    group['sheets'][0]['placements'] = [
        {
            'partId': f'p{i}',
            'originalInstance': 0,
            'loops': [rectangle((i % 10) + 0.5, (i // 10) * 0.7 + 0.25, 0.5, 0.5)],
        }
        for i in range(100)
    ]
    result = pdf(value)
    (tmp_path / 'buyer-100.pdf').write_bytes(result)
    reader = read(result)
    text = '\n'.join(page.extract_text() for page in reader.pages)
    assert len(reader.pages) >= 5
    assert all(f'Original drawing {i:03}' in text for i in range(100))
    assert text.count('On this sheet') >= 2
    assert 'P099' in text
    assert all('Project: Synthetic nesting project' in page.extract_text() for page in reader.pages)
    assert text.count('Input fingerprint (SHA-256)') == 1
    assert 'Job aaaaaaaaaaaa' not in text


def test_long_project_reference_is_single_line_and_fitted_on_every_page():
    value = report()
    value['projectName'] = 'Buyer job\n  ref ' + 'W' * 175
    reader = read(pdf(value))
    for page in reader.pages:
        text = page.extract_text()
        assert 'Project: Buyer job ref ' in text
        header = next(line for line in text.splitlines() if line.startswith('Project:'))
        assert header.endswith('...')
        assert len(header) < len(value['projectName'])


def test_subnormal_sheet_dimensions_are_rejected_before_pdf_scaling():
    value = report()
    group = value['groups'][0]
    group['selectionKind'] = 'recorded_piece'
    group['baselinePurchaseSheets'] = [{'widthIn': 8, 'lengthIn': 12, 'quantity': 1}]
    sheet = group['sheets'][0]
    sheet.update(source='recorded_piece', widthIn=1e-320, lengthIn=1e-320, marginIn=0, gapIn=0)
    sheet['outer'] = {'type': 'circle', 'cx': 5e-321, 'cy': 5e-321, 'r': 5e-321}
    sheet['placements'] = [
        {'partId': 'p', 'originalInstance': i, 'loops': [{'type': 'circle', 'cx': 3e-321, 'cy': 3e-321, 'r': 1e-321}]}
        for i in range(2)
    ]
    with pytest.raises(ValueError):
        BuyerPdfReport.model_validate(value)


def test_measurement_display_omits_normalization_noise_without_mutating_geometry():
    value = report()
    sheet = value['groups'][0]['sheets'][0]
    sheet['lengthIn'] = 12.000000001
    sheet['outer']['points'][1]['x'] = sheet['lengthIn']
    sheet['outer']['points'][2]['x'] = sheet['lengthIn']
    before = copy.deepcopy(value)
    text = '\n'.join(page.extract_text() for page in read(pdf(value)).pages)
    assert '8 x 12' in text and '12.000000001' not in text
    assert value == before
