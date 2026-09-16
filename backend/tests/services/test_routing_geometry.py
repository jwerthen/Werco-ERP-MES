"""Routing geometry shares the qualified parser without inventing flat blanks."""

from app.services.routing_geometry import inspect_routing_geometry


def test_dxf_physical_units_are_converted_to_routing_inches(tmp_path, monkeypatch):
    path = tmp_path / 'flat.dxf'
    path.write_bytes(b'fixture')
    monkeypatch.setattr(
        'app.services.routing_geometry.analyze_in_worker',
        lambda *args: {
            'kind': 'dxf',
            'geometry': {
                'units': 'mm',
                'net_candidate_area_mm2': 645.16,
                'measured_length_mm': 101.6,
                'bounds_mm': [0, 0, 25.4, 25.4],
            },
        },
    )
    result = inspect_routing_geometry(str(path), path.name)
    assert result['flat_area'] == 1 and result['cut_length'] == 4
    assert result['bbox'] == {'min_x': 0, 'min_y': 0, 'max_x': 1, 'max_y': 1}
    assert result['bend_count'] is None and result['low_confidence'] is True


def test_step_bounds_do_not_become_flat_area_or_cut_length(tmp_path, monkeypatch):
    path = tmp_path / 'assembly.step'
    path.write_bytes(b'fixture')
    monkeypatch.setattr(
        'app.services.routing_geometry.analyze_in_worker',
        lambda *args: {
            'kind': 'step',
            'geometry': {'bounds_mm': [0, 0, 0, 50, 100, 200]},
        },
    )
    result = inspect_routing_geometry(str(path), path.name)
    assert result['flat_area'] is None and result['cut_length'] is None
    assert result['bbox'] is None and result['bend_count'] is None
