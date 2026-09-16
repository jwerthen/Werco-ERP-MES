"""Exercise the packaged CAD/document interpreter without database or network access."""

import io
import os
import sys
import tempfile
from pathlib import Path

import ezdxf
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
from pypdf import PdfWriter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.fabrication_quote.worker import analyze_in_worker, nest_in_worker  # noqa: E402


def main():
    interpreter = os.environ['WERCO_QUOTE_WORKER_PYTHON']
    assert Path(interpreter).samefile(sys.executable), 'Run using the packaged quotation interpreter'
    drawing = ezdxf.new()
    drawing.units = 4
    drawing.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 50), (0, 50)], close=True)
    stream = io.StringIO()
    drawing.write(stream)
    dxf = analyze_in_worker(stream.getvalue().encode(), 'smoke.dxf')
    assert dxf['kind'] == 'dxf' and dxf['geometry'], dxf
    with tempfile.TemporaryDirectory() as directory:
        step_path = Path(directory) / 'plate.step'
        writer = STEPControl_Writer()
        writer.Transfer(BRepPrimAPI_MakeBox(100, 50, 2).Shape(), STEPControl_AsIs)
        assert writer.Write(str(step_path)) == IFSelect_RetDone
        step = analyze_in_worker(step_path.read_bytes(), step_path.name)
    assert step['kind'] == 'step' and step['geometry']['definitions'], step
    assert step['geometry']['meshes'], 'The STEP worker must produce display geometry'
    assert step['geometry']['definitions'][0]['flat_pattern']['status'] == 'candidate', step
    pdf_writer = PdfWriter()
    pdf_writer.add_blank_page(width=200, height=100)
    pdf_stream = io.BytesIO()
    pdf_writer.write(pdf_stream)
    pdf = analyze_in_worker(pdf_stream.getvalue(), 'smoke.pdf')
    assert pdf['kind'] == 'pdf' and len(pdf['pages']) == 1, pdf
    csv = analyze_in_worker(b'MPN,Qty\n000123,4\n', 'smoke.csv')
    assert csv['table']['rows'][1]['cells'] == ['000123', '4'], csv
    nest = nest_in_worker(
        {
            'parts': [
                {'id': 'plate', 'quantity': 1, 'width_mm': 100, 'height_mm': 50, 'material': 'steel', 'thickness_mm': 2}
            ],
            'stocks': [
                {
                    'id': 'sheet',
                    'quantity': 1,
                    'width_mm': 500,
                    'height_mm': 500,
                    'material': 'steel',
                    'thickness_mm': 2,
                    'price': 20,
                    'currency': 'USD',
                }
            ],
            'spacing_mm': 2,
            'edge_margin_mm': 5,
        }
    )
    assert nest['status'] == 'complete' and nest['validated'], nest
    print('Packaged quotation worker verified: DXF, STEP/mesh/flat candidate, PDF, CSV and nesting.')


if __name__ == '__main__':
    main()
