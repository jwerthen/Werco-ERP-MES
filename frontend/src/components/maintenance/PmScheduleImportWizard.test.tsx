import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import api from '../../services/api';
import { PmScheduleImportWizard, formatPmChangeValue } from './PmScheduleImportWizard';
import type { PmImportResponse } from '../../types/maintenance';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    previewPmScheduleImport: jest.fn(),
    commitPmScheduleImport: jest.fn(),
    downloadImportTemplate: jest.fn(),
  },
}));
const mockedApi = api as jest.Mocked<typeof api>;

const HASH = 'b'.repeat(64);
const file = new File(['csv'], 'pm-schedule.csv', { type: 'text/csv' });

const preview = (overrides: Partial<PmImportResponse['summary']> = {}): PmImportResponse => ({
  filename: file.name,
  source_hash: HASH,
  committed: false,
  summary: {
    total_rows: 5,
    task_rows: 4,
    skipped_rows: 1,
    error_count: 0,
    warning_count: 1,
    schedules_to_create: 1,
    schedules_to_update: 1,
    schedules_unchanged: 0,
    assets_to_create: 1,
    ...overrides,
  },
  rows: [
    {
      row: 2,
      target_type: 'work_center',
      target_code: 'CNC-01',
      task: 'Lubricate ways',
      outcome: 'update',
      warnings: [],
    },
    {
      row: 6,
      target_type: 'work_center',
      target_code: 'CNC-01',
      task: 'Clean chip conveyor',
      outcome: 'skipped',
      warnings: ["Frequency 'as necessary' has no cadence; the row is skipped."],
    },
  ],
  schedules: [
    {
      schedule_id: 11,
      name: 'CNC-01 Monthly PM',
      target_type: 'work_center',
      target_id: 1,
      target_code: 'CNC-01',
      target_name: 'Mori Seiki MV-653',
      frequency: 'monthly',
      tasks: ['Lubricate ways', 'Check coolant level'],
      outcome: 'update',
      changes: { checklist: { old: ['Lubricate ways'], new: ['Lubricate ways', 'Check coolant level'] } },
      rows: [2, 3],
    },
    {
      name: 'FL-01 Annual PM',
      target_type: 'asset',
      target_code: 'FL-01',
      target_name: 'Forklift 1',
      frequency: 'annual',
      tasks: ['Replace battery'],
      outcome: 'create',
      changes: {},
      rows: [4],
    },
  ],
  assets_to_create: [{ asset_tag: 'FL-01', name: 'Forklift 1', asset_type: 'forklift', rows: [4] }],
});

const upload = async () => {
  fireEvent.change(screen.getByLabelText(/File \(\.xlsx or \.csv\)/), { target: { files: [file] } });
  fireEvent.click(screen.getByRole('button', { name: /Preview \(dry run\)/ }));
  await screen.findByRole('heading', { name: 'Review PM schedules' });
};

beforeEach(() => jest.clearAllMocks());

describe('PmScheduleImportWizard', () => {
  it('summarizes checklist changes as task counts', () => {
    expect(formatPmChangeValue('checklist', ['a', 'b'])).toBe('2 tasks');
    expect(formatPmChangeValue('requires_shutdown', false)).toBe('No');
    expect(formatPmChangeValue('frequency', 'semi_annual')).toBe('semi annual');
  });

  it('previews schedules, the assets it will create and the skipped rows, then commits with the hash', async () => {
    mockedApi.previewPmScheduleImport.mockResolvedValue(preview());
    mockedApi.commitPmScheduleImport.mockResolvedValue({ ...preview(), committed: true });
    const onCommitted = jest.fn();
    render(<PmScheduleImportWizard onClose={jest.fn()} onCommitted={onCommitted} />);
    await upload();

    const assets = screen.getByRole('region', { name: 'Assets to create' });
    expect(within(assets).getByText('FL-01')).toBeInTheDocument();
    const schedules = screen.getByRole('region', { name: 'Schedules' });
    const cnc = within(schedules).getByText('CNC-01 Monthly PM').closest('tr') as HTMLElement;
    expect(within(cnc).getByText('Check coolant level')).toBeInTheDocument();
    expect(within(cnc).getByText('2 tasks')).toBeInTheDocument();
    expect(within(cnc).getByText('Monthly')).toBeInTheDocument();
    const forklift = within(schedules).getByText('FL-01 Annual PM').closest('tr') as HTMLElement;
    expect(within(forklift).getByText('Asset')).toBeInTheDocument();
    expect(within(forklift).getByText('create')).toBeInTheDocument();
    const issues = screen.getByRole('region', { name: 'Row issues' });
    expect(within(issues).getByText(/as necessary/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Commit import/ }));
    await screen.findByRole('heading', { name: 'PM schedules imported' });
    expect(mockedApi.commitPmScheduleImport).toHaveBeenCalledWith(file, HASH);
    expect(onCommitted).toHaveBeenCalled();
  });

  it('blocks the commit while any row has an error', async () => {
    mockedApi.previewPmScheduleImport.mockResolvedValue(preview({ error_count: 2 }));
    render(<PmScheduleImportWizard onClose={jest.fn()} onCommitted={jest.fn()} />);
    await upload();
    expect(screen.getByText(/2 rows have errors/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Commit import/ })).toBeDisabled();
  });

  it('shows the all-or-nothing 409 row errors', async () => {
    mockedApi.previewPmScheduleImport.mockResolvedValue(preview());
    mockedApi.commitPmScheduleImport.mockRejectedValue({
      response: {
        status: 409,
        data: {
          detail: {
            message: '1 row(s) have errors; nothing was imported. Fix them and preview again.',
            errors: [{ row: 4, target: 'XYZ-9', task: 'Grease', error: "Unknown work center code 'XYZ-9'" }],
          },
        },
      },
    });
    render(<PmScheduleImportWizard onClose={jest.fn()} onCommitted={jest.fn()} />);
    await upload();
    fireEvent.click(screen.getByRole('button', { name: /Commit import/ }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent("Row 4 (XYZ-9): Unknown work center code 'XYZ-9'");
  });
});
