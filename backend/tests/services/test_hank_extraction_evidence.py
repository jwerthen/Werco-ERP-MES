"""Receipt-critical extracted values must be supported by their cited page text."""

from types import SimpleNamespace

import pytest

from app.services import hank_intake_service as intake


@pytest.mark.parametrize('quantity,expected', [('12', 'high'), ('2', 'low'), ('99', 'low')])
def test_receipt_quantity_must_occur_as_whole_value_in_evidence(monkeypatch, quantity, expected):
    text = 'PART-10 delivered 12 EA Heat H-4 Lot L-8'
    monkeypatch.setattr(intake, 'extract_intake', lambda _: [text])
    monkeypatch.setattr(
        intake,
        'run_llm_task',
        lambda *args, **kwargs: SimpleNamespace(
            raw_response=SimpleNamespace(
                stop_reason='tool_use',
                content=[
                    SimpleNamespace(
                        type='tool_use',
                        name='record_intake',
                        input={
                            'classification': 'packing_slip',
                            'confidence': 'high',
                            'summary': 'Delivered material',
                            'evidence': [{'page': 1, 'excerpt': text}],
                            'lines': [
                                {
                                    'part_number': 'PART-10',
                                    'quantity': quantity,
                                    'unit_of_measure': 'EA',
                                    'heat_number': 'H-4',
                                    'lot_number': 'L-8',
                                    'confidence': 'high',
                                    'evidence': [{'page': 1, 'excerpt': text}],
                                }
                            ],
                        },
                    )
                ],
            )
        ),
    )
    extraction, _ = intake.analyze_pdf(b'source', 1)
    assert extraction.lines[0].confidence == expected


def test_truncated_extraction_is_not_saved_as_complete(monkeypatch):
    monkeypatch.setattr(intake, 'extract_intake', lambda _: ['Delivered material'])
    monkeypatch.setattr(
        intake,
        'run_llm_task',
        lambda *args, **kwargs: SimpleNamespace(raw_response=SimpleNamespace(stop_reason='max_tokens', content=[])),
    )
    with pytest.raises(ValueError, match='incomplete'):
        intake.analyze_pdf(b'source', 1)
