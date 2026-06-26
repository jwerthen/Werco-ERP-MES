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
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

/** A two-row preview: one clean high-confidence PDF, one low-confidence PDF. */
const preview: LaserNestPackagePreview = {
  package_name: 'nests.zip',
  nest_count: 2,
  total_planned_runs: 7,
  nests: [
    {
      source_file: 'sheet-1.pdf',
      nest_name: 'Sheet 1',
      cnc_number: '8001',
      cnc_file_name: null,
      planned_runs: 5,
      material: '304 SS',
      thickness: '0.125"',
      sheet_size: '48x96',
      confidence: 'high',
    },
    {
      source_file: 'sheet-2.pdf',
      nest_name: 'Sheet 2',
      cnc_number: '8002',
      cnc_file_name: null,
      planned_runs: 2,
      material: 'AL 6061',
      thickness: '0.090"',
      sheet_size: '48x96',
      confidence: 'low',
    },
  ],
};

/** Pick a ZIP, run Preview, and wait for the editable grid to render. */
async function previewPackage() {
  const zip = new File(['PK'], 'nests.zip', { type: 'application/zip' });
  fireEvent.change(screen.getByLabelText(/zip package/i), { target: { files: [zip] } });
  fireEvent.click(screen.getByRole('button', { name: /^preview$/i }));
  await waitFor(() => expect(mockApi.previewLaserNestPackage).toHaveBeenCalled());
  // The grid headers only appear on the review step.
  await screen.findByRole('button', { name: /^import 2 nests$/i });
}

describe('LaserNestImportWizard', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockApi.previewLaserNestPackage.mockResolvedValue(preview);
    mockApi.importLaserNestPackage.mockResolvedValue({ child_work_order: { id: 909 } });
  });

  it('previews the package and renders an editable row per nest', async () => {
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();

    expect(mockApi.previewLaserNestPackage).toHaveBeenCalledWith(
      42,
      expect.objectContaining({ file: expect.any(File) })
    );

    // Each row's CNC number is pre-filled into an editable cell keyed by source file.
    expect(screen.getByLabelText('CNC number for sheet-1.pdf')).toHaveValue('8001');
    expect(screen.getByLabelText('Material for sheet-1.pdf')).toHaveValue('304 SS');
    expect(screen.getByLabelText('Runs for sheet-1.pdf')).toHaveValue(5);
    expect(screen.getByLabelText('CNC number for sheet-2.pdf')).toHaveValue('8002');
    // The low-confidence row is flagged.
    expect(screen.getByText(/1 low-confidence/i)).toBeInTheDocument();
  });

  it('editing a cell updates only that row', async () => {
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();

    fireEvent.change(screen.getByLabelText('Material for sheet-1.pdf'), { target: { value: 'Titanium' } });

    expect(screen.getByLabelText('Material for sheet-1.pdf')).toHaveValue('Titanium');
    // Sibling row untouched.
    expect(screen.getByLabelText('Material for sheet-2.pdf')).toHaveValue('AL 6061');
  });

  it('Remove drops a row and updates the import button count/label', async () => {
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();

    fireEvent.click(screen.getByRole('button', { name: /remove sheet-2\.pdf/i }));

    // Row gone, count + button label drop to one (and singularize).
    expect(screen.queryByLabelText('CNC number for sheet-2.pdf')).not.toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /^import 1 nest$/i })).toBeInTheDocument();
    expect(screen.getByText(/^1 nest$/)).toBeInTheDocument();
  });

  it('imports the confirmed rows with source_file and edited values', async () => {
    const onImported = jest.fn();
    render(<LaserNestImportWizard open workOrderId={42} workCenterId={3} onClose={jest.fn()} onImported={onImported} />);
    await previewPackage();

    // Edit one cell, then drop the second row so only the edited row imports.
    fireEvent.change(screen.getByLabelText('Material for sheet-1.pdf'), { target: { value: 'Inconel' } });
    fireEvent.change(screen.getByLabelText('Runs for sheet-1.pdf'), { target: { value: '8' } });
    fireEvent.click(screen.getByRole('button', { name: /remove sheet-2\.pdf/i }));

    fireEvent.click(await screen.findByRole('button', { name: /^import 1 nest$/i }));

    await waitFor(() => expect(mockApi.importLaserNestPackage).toHaveBeenCalledTimes(1));
    const [woId, payload] = mockApi.importLaserNestPackage.mock.calls[0];
    expect(woId).toBe(42);
    expect(payload.work_center_id).toBe(3);
    expect(payload.rows).toEqual([
      expect.objectContaining({
        source_file: 'sheet-1.pdf',
        cnc_number: '8001',
        material: 'Inconel',
        planned_runs: 8,
      }),
    ]);
    // The dropped row is not sent.
    expect(payload.rows?.some((r) => r.source_file === 'sheet-2.pdf')).toBe(false);
    // On success the parent is handed the created child WO id.
    await waitFor(() => expect(onImported).toHaveBeenCalledWith(909));
  });

  it('blocks import and surfaces the offending row when a CNC number is blank', async () => {
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();

    fireEvent.change(screen.getByLabelText('CNC number for sheet-1.pdf'), { target: { value: '   ' } });
    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    expect(await screen.findByText(/enter a cnc number for sheet-1\.pdf/i)).toBeInTheDocument();
    expect(mockApi.importLaserNestPackage).not.toHaveBeenCalled();
  });
});
