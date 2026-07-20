/**
 * LaserNestImportWizard — bare (multi-page) PDF upload flow.
 *
 * A bare-PDF preview returns package metadata (`source_page_count`,
 * `skipped_pages`, `segmentation_warning`) and per-row PDF extras
 * (`source_pages`, `field_confidence`, `warning`). The wizard must:
 *   - show the "N pages → M nests" chip, the skipped-pages note, and the
 *     amber segmentation-warning chip;
 *   - show each row's page range ("p. 1–2" / "p. 3") instead of the generated
 *     segment file name, and a warning icon for rows with a `warning`;
 *   - amber-flag low-confidence fields (field_confidence === 'low' OR a blank
 *     value on a PDF row) until the planner edits that specific field;
 *   - echo `source_pages` back VERBATIM in the import rows.
 *
 * The ZIP flow is regression-guarded: no PDF chips, no blank-field flags, and
 * the import payload carries NO `source_pages` key.
 *
 * Mirrors LaserNestImportWizard.test.tsx patterns (mocked api module).
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import LaserNestImportWizard from './LaserNestImportWizard';
import { LaserNestPackagePreview } from '../../types';
import api from '../../services/api';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    previewLaserNestPackage: jest.fn(),
    importLaserNestPackage: jest.fn(),
    previewLaserNestPackageStandalone: jest.fn(),
    importLaserNestPackageStandalone: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const ROW_WARNING = 'Verification pass skipped: API error: connection reset';

/** A 4-page bare-PDF preview segmented into two nests; page 4 skipped. */
const pdfPreview: LaserNestPackagePreview = {
  package_name: 'nests.pdf',
  nest_count: 2,
  total_planned_runs: 5,
  source_page_count: 4,
  skipped_pages: [4],
  segmentation_warning: null,
  nests: [
    {
      source_file: 'nest-p001-p002.pdf',
      nest_name: '8001',
      cnc_number: '8001',
      cnc_file_name: 'nest-p001-p002.pdf',
      planned_runs: 3,
      material: 'A36',
      thickness: '0.25"',
      sheet_size: '72.5x120',
      confidence: 'low',
      source_pages: [1, 2],
      field_confidence: { cnc_number: 'high', material: 'low', thickness: 'high' },
      warning: ROW_WARNING,
      passes: 1,
    },
    {
      source_file: 'nest-p003.pdf',
      nest_name: '8002',
      cnc_number: '8002',
      cnc_file_name: 'nest-p003.pdf',
      planned_runs: 2,
      material: '304 SS',
      thickness: '10ga',
      sheet_size: null, // blank on a PDF row -> needs-verify highlight
      confidence: 'high',
      source_pages: [3],
      field_confidence: { cnc_number: 'high', material: 'high' },
      warning: null,
      passes: 2,
    },
  ],
};

/** A plain ZIP preview (no bare-PDF extras at all). */
const zipPreview: LaserNestPackagePreview = {
  package_name: 'nests.zip',
  nest_count: 1,
  total_planned_runs: 5,
  nests: [
    {
      source_file: 'sheet-1.pdf',
      nest_name: 'Sheet 1',
      cnc_number: '9001',
      cnc_file_name: null,
      planned_runs: 5,
      material: '', // blank — but NOT a PDF row, so no verify highlight
      thickness: '0.125"',
      sheet_size: '48x96',
      confidence: 'high',
    },
  ],
};

/** Pick a file, run Preview, and wait for the review grid. */
async function previewFile(file: File, importButton: RegExp) {
  fireEvent.change(screen.getByLabelText(/zip package or nest-report pdf/i), { target: { files: [file] } });
  fireEvent.click(screen.getByRole('button', { name: /^preview$/i }));
  await waitFor(() => expect(mockApi.previewLaserNestPackage).toHaveBeenCalled());
  await screen.findByRole('button', { name: importButton });
}

const pdfFile = () => new File(['%PDF-1.4'], 'nests.pdf', { type: 'application/pdf' });
const zipFile = () => new File(['PK'], 'nests.zip', { type: 'application/zip' });

describe('LaserNestImportWizard — bare-PDF preview metadata', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockApi.previewLaserNestPackage.mockResolvedValue(pdfPreview);
    mockApi.importLaserNestPackage.mockResolvedValue({ child_work_order: { id: 909 } });
  });

  it('shows the pages→nests chip and the skipped-pages note', async () => {
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewFile(pdfFile(), /^import 2 nests$/i);

    // "4 pages → 2 nests" chip (text is interpolated across one span).
    expect(
      screen.getByText((_, element) => element?.tagName === 'SPAN' && /4 pages\s*→\s*2 nests/.test(element.textContent ?? ''))
    ).toBeInTheDocument();
    expect(screen.getByText(/pages skipped as non-nest: 4/i)).toBeInTheDocument();
  });

  it('shows the amber segmentation warning chip when segmentation degraded', async () => {
    const warning = 'AI segmentation response failed validation; defaulted to one nest per page';
    mockApi.previewLaserNestPackage.mockResolvedValue({ ...pdfPreview, segmentation_warning: warning });
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewFile(pdfFile(), /^import 2 nests$/i);

    expect(screen.getByText(warning)).toBeInTheDocument();
  });

  it('omits the pages chip and skipped note when the preview has no PDF metadata (ZIP flow)', async () => {
    mockApi.previewLaserNestPackage.mockResolvedValue(zipPreview);
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewFile(zipFile(), /^import 1 nest$/i);

    expect(screen.queryByText(/pages\s*→/)).not.toBeInTheDocument();
    expect(screen.queryByText(/pages skipped as non-nest/i)).not.toBeInTheDocument();
  });
});

describe('LaserNestImportWizard — per-row PDF display', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockApi.previewLaserNestPackage.mockResolvedValue(pdfPreview);
    mockApi.importLaserNestPackage.mockResolvedValue({ child_work_order: { id: 909 } });
  });

  it('shows each row as its page range, keeping the file name as tooltip', async () => {
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewFile(pdfFile(), /^import 2 nests$/i);

    const range = screen.getByText('p. 1–2');
    expect(range).toBeInTheDocument();
    expect(range).toHaveAttribute('title', 'nest-p001-p002.pdf');
    expect(screen.getByText('p. 3')).toBeInTheDocument();
  });

  it('renders an accessible warning icon only for rows carrying a warning', async () => {
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewFile(pdfFile(), /^import 2 nests$/i);

    const icons = screen.getAllByRole('img');
    expect(icons).toHaveLength(1); // second row has warning: null
    expect(icons[0]).toHaveAccessibleName(`Warning for nest-p001-p002.pdf: ${ROW_WARNING}`);
  });
});

describe('LaserNestImportWizard — low-confidence field highlighting', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockApi.previewLaserNestPackage.mockResolvedValue(pdfPreview);
    mockApi.importLaserNestPackage.mockResolvedValue({ child_work_order: { id: 909 } });
  });

  it("flags field_confidence==='low' fields and blank PDF-row fields, not the rest", async () => {
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewFile(pdfFile(), /^import 2 nests$/i);

    // Row 1's material was merged at low confidence -> verify affordance.
    expect(
      screen.getByLabelText('Material for nest-p001-p002.pdf — low confidence, verify')
    ).toBeInTheDocument();
    // Row 1's cnc_number/thickness merged high -> plain label.
    expect(screen.getByLabelText('CNC number for nest-p001-p002.pdf')).toBeInTheDocument();
    expect(screen.getByLabelText('Thickness for nest-p001-p002.pdf')).toBeInTheDocument();
    // Row 2's sheet_size is BLANK on a PDF row -> flagged even without a 'low'.
    expect(
      screen.getByLabelText('Sheet size for nest-p003.pdf — low confidence, verify')
    ).toBeInTheDocument();
    // Row 2's material merged high -> plain.
    expect(screen.getByLabelText('Material for nest-p003.pdf')).toBeInTheDocument();
  });

  it('clears the flag for a field once the planner edits it — other fields stay flagged', async () => {
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewFile(pdfFile(), /^import 2 nests$/i);

    fireEvent.change(screen.getByLabelText('Material for nest-p001-p002.pdf — low confidence, verify'), {
      target: { value: 'A572' },
    });

    // The edited field's verify affordance is gone; the value stuck.
    expect(screen.queryByLabelText(/material for nest-p001-p002\.pdf — low confidence/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText('Material for nest-p001-p002.pdf')).toHaveValue('A572');
    // The OTHER row's flagged field is untouched.
    expect(
      screen.getByLabelText('Sheet size for nest-p003.pdf — low confidence, verify')
    ).toBeInTheDocument();
  });

  it('does not flag blank fields on non-PDF (ZIP/CNC) rows', async () => {
    mockApi.previewLaserNestPackage.mockResolvedValue(zipPreview);
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewFile(zipFile(), /^import 1 nest$/i);

    // Material is blank but the row has no source_pages -> no verify suffix.
    expect(screen.getByLabelText('Material for sheet-1.pdf')).toHaveValue('');
    expect(screen.queryByLabelText(/low confidence, verify/)).not.toBeInTheDocument();
  });
});

describe('LaserNestImportWizard — import payload', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockApi.previewLaserNestPackage.mockResolvedValue(pdfPreview);
    mockApi.importLaserNestPackage.mockResolvedValue({ child_work_order: { id: 909 } });
  });

  it('echoes source_pages back verbatim for every PDF row (even after edits)', async () => {
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewFile(pdfFile(), /^import 2 nests$/i);

    // Editing a field must not disturb the page lists.
    fireEvent.change(screen.getByLabelText('Material for nest-p001-p002.pdf — low confidence, verify'), {
      target: { value: 'A572' },
    });
    fireEvent.change(screen.getByLabelText('Sheet size for nest-p003.pdf — low confidence, verify'), {
      target: { value: '48x96' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    await waitFor(() => expect(mockApi.importLaserNestPackage).toHaveBeenCalledTimes(1));
    const [, payload] = mockApi.importLaserNestPackage.mock.calls[0];
    expect(payload.rows).toEqual([
      expect.objectContaining({
        source_file: 'nest-p001-p002.pdf',
        cnc_number: '8001',
        material: 'A572',
        planned_runs: 3,
        source_pages: [1, 2],
      }),
      expect.objectContaining({
        source_file: 'nest-p003.pdf',
        cnc_number: '8002',
        sheet_size: '48x96',
        planned_runs: 2,
        source_pages: [3],
      }),
    ]);
  });

  it('ZIP rows carry NO source_pages key at all', async () => {
    mockApi.previewLaserNestPackage.mockResolvedValue(zipPreview);
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewFile(zipFile(), /^import 1 nest$/i);

    fireEvent.click(screen.getByRole('button', { name: /^import 1 nest$/i }));

    await waitFor(() => expect(mockApi.importLaserNestPackage).toHaveBeenCalledTimes(1));
    const [, payload] = mockApi.importLaserNestPackage.mock.calls[0];
    expect(payload.rows).toHaveLength(1);
    expect(payload.rows?.[0]).toEqual(
      expect.objectContaining({ source_file: 'sheet-1.pdf', cnc_number: '9001', planned_runs: 5 })
    );
    // Absent, not null/undefined-valued: the key itself must not be sent.
    expect(payload.rows?.[0]).not.toHaveProperty('source_pages');
  });
});
