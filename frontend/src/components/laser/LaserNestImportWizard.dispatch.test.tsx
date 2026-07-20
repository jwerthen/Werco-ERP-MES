/**
 * LaserNestImportWizard — dispatch controls (due date + work centers).
 *
 * STANDALONE review step grows a "Dispatch" strip:
 *  - an optional "Due date" input (default EMPTY — no fabricated promise
 *    dates) sent as FormData `due_date` only when set;
 *  - a "Work center" select of ACTIVE work centers in laser-first order,
 *    DEFAULTED to the Ermaksan fiber laser (owner decision: never the HSG
 *    tube laser), with an explicit "(auto-detect)" option that omits the
 *    field entirely.
 *
 * BOTH modes grow a per-row "WC" override column; a set override rides on
 * that row's confirmed LaserNestImportRow as `work_center_id`, an unset one
 * sends NO key. Parented mode keeps the package-level pick on the
 * `workCenterId` prop and never shows the strip.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import LaserNestImportWizard from './LaserNestImportWizard';
import { LaserNestPackagePreview, WorkCenter } from '../../types';
import api from '../../services/api';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    previewLaserNestPackage: jest.fn(),
    importLaserNestPackage: jest.fn(),
    previewLaserNestPackageStandalone: jest.fn(),
    importLaserNestPackageStandalone: jest.fn(),
    getWorkCenters: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const workCenter = (overrides: Partial<WorkCenter>): WorkCenter => ({
  id: 1,
  version: 1,
  code: 'WC-1',
  name: 'Work Center',
  work_center_type: 'fabrication',
  hourly_rate: 100,
  capacity_hours_per_day: 8,
  efficiency_factor: 1,
  is_active: true,
  current_status: 'available',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  ...overrides,
});

const ERMAKSAN = workCenter({ id: 5, name: 'Ermaksan Fiber Laser', code: 'LSR-1', work_center_type: 'laser_cutting' });
const TUBE = workCenter({ id: 6, name: 'HSG Tube Laser', code: 'LSR-2', work_center_type: 'laser_cutting' });
const BRAKE = workCenter({ id: 7, name: 'Brake Press', code: 'BRK-1', work_center_type: 'forming' });
const INACTIVE = workCenter({ id: 8, name: 'Retired Laser', code: 'LSR-0', is_active: false });

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
      confidence: 'high',
    },
  ],
};

/** Pick a ZIP, run Preview, and wait for the review grid. */
async function previewPackage(previewMock: jest.Mock) {
  const zip = new File(['PK'], 'nests.zip', { type: 'application/zip' });
  fireEvent.change(screen.getByLabelText(/zip package/i), { target: { files: [zip] } });
  fireEvent.click(screen.getByRole('button', { name: /^preview$/i }));
  await waitFor(() => expect(previewMock).toHaveBeenCalled());
  await screen.findByRole('button', { name: /^import 2 nests$/i });
}

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getWorkCenters.mockResolvedValue([TUBE, BRAKE, INACTIVE, ERMAKSAN]);
  mockApi.previewLaserNestPackageStandalone.mockResolvedValue(preview);
  mockApi.importLaserNestPackageStandalone.mockResolvedValue({
    package: preview,
    child_work_order: { id: 1201, work_order_number: 'WO-1201' },
  });
  mockApi.previewLaserNestPackage.mockResolvedValue(preview);
  mockApi.importLaserNestPackage.mockResolvedValue({ child_work_order: { id: 909 } });
});

describe('standalone dispatch strip', () => {
  it('defaults the work center to the Ermaksan fiber laser — never the tube laser — with an empty due date', async () => {
    render(<LaserNestImportWizard open onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage(mockApi.previewLaserNestPackageStandalone as unknown as jest.Mock);

    expect(screen.getByLabelText('Due date')).toHaveValue('');
    const select = screen.getByLabelText('Work center') as HTMLSelectElement;
    await waitFor(() => expect(select).toHaveValue('5'));
    expect(select.selectedOptions[0]).toHaveTextContent('Ermaksan Fiber Laser');
    // Auto-detect stays available; inactive centers are excluded.
    expect(within_options(select)).toEqual(
      expect.arrayContaining(['(auto-detect)', 'Ermaksan Fiber Laser', 'HSG Tube Laser', 'Brake Press'])
    );
    expect(within_options(select)).not.toContain('Retired Laser');
  });

  it('sends due_date and the picked work_center_id on import', async () => {
    render(<LaserNestImportWizard open onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage(mockApi.previewLaserNestPackageStandalone as unknown as jest.Mock);

    fireEvent.change(screen.getByLabelText('Due date'), { target: { value: '2026-08-01' } });
    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    await waitFor(() => expect(mockApi.importLaserNestPackageStandalone).toHaveBeenCalledTimes(1));
    const [payload] = mockApi.importLaserNestPackageStandalone.mock.calls[0];
    expect(payload.due_date).toBe('2026-08-01');
    expect(payload.work_center_id).toBe(5);
  });

  it('omits both fields when the due date is empty and "(auto-detect)" is picked', async () => {
    render(<LaserNestImportWizard open onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage(mockApi.previewLaserNestPackageStandalone as unknown as jest.Mock);
    await waitFor(() => expect(screen.getByLabelText('Work center')).toHaveValue('5'));

    fireEvent.change(screen.getByLabelText('Work center'), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    await waitFor(() => expect(mockApi.importLaserNestPackageStandalone).toHaveBeenCalledTimes(1));
    const [payload] = mockApi.importLaserNestPackageStandalone.mock.calls[0];
    expect(payload.due_date).toBeUndefined();
    expect(payload.work_center_id).toBeUndefined();
  });

  it('keeps auto-detect as the default when the only laser is a tube laser', async () => {
    mockApi.getWorkCenters.mockResolvedValue([TUBE, BRAKE]);
    render(<LaserNestImportWizard open onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage(mockApi.previewLaserNestPackageStandalone as unknown as jest.Mock);

    expect(screen.getByLabelText('Work center')).toHaveValue('');
  });
});

describe('per-row WC override column', () => {
  it('sends work_center_id only on rows the planner overrode (standalone)', async () => {
    render(<LaserNestImportWizard open onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage(mockApi.previewLaserNestPackageStandalone as unknown as jest.Mock);

    // Row 1 → tube laser override; row 2 stays on "package default".
    fireEvent.change(screen.getByLabelText('Work center for sheet-1.pdf'), { target: { value: '6' } });
    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    await waitFor(() => expect(mockApi.importLaserNestPackageStandalone).toHaveBeenCalledTimes(1));
    const [payload] = mockApi.importLaserNestPackageStandalone.mock.calls[0];
    expect(payload.rows?.[0]).toEqual(expect.objectContaining({ source_file: 'sheet-1.pdf', work_center_id: 6 }));
    // Absent, not null-valued: unset overrides must not send the key.
    expect(payload.rows?.[1]).not.toHaveProperty('work_center_id');
  });

  it('parented mode: no dispatch strip, but per-row overrides still ride the rows (package pick stays on the prop)', async () => {
    render(<LaserNestImportWizard open workOrderId={42} workCenterId={3} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage(mockApi.previewLaserNestPackage as unknown as jest.Mock);

    // Strip is standalone-only.
    expect(screen.queryByLabelText('Due date')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Work center')).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Work center for sheet-2.pdf'), { target: { value: '5' } });
    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    await waitFor(() => expect(mockApi.importLaserNestPackage).toHaveBeenCalledTimes(1));
    const [woId, payload] = mockApi.importLaserNestPackage.mock.calls[0];
    expect(woId).toBe(42);
    expect(payload.work_center_id).toBe(3); // prop, unchanged
    expect(payload.rows?.[0]).not.toHaveProperty('work_center_id');
    expect(payload.rows?.[1]).toEqual(expect.objectContaining({ source_file: 'sheet-2.pdf', work_center_id: 5 }));
  });

  it('an override can be cleared back to package default', async () => {
    render(<LaserNestImportWizard open onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage(mockApi.previewLaserNestPackageStandalone as unknown as jest.Mock);

    const rowSelect = screen.getByLabelText('Work center for sheet-1.pdf');
    fireEvent.change(rowSelect, { target: { value: '6' } });
    fireEvent.change(rowSelect, { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    await waitFor(() => expect(mockApi.importLaserNestPackageStandalone).toHaveBeenCalledTimes(1));
    const [payload] = mockApi.importLaserNestPackageStandalone.mock.calls[0];
    expect(payload.rows?.[0]).not.toHaveProperty('work_center_id');
  });
});

/** Option labels of a <select>, in DOM order. */
function within_options(select: HTMLSelectElement): string[] {
  return Array.from(select.options).map((option) => option.textContent ?? '');
}
