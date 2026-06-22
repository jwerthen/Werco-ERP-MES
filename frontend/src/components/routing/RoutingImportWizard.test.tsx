import React from 'react';
import { fireEvent, renderWithRouter, screen, waitFor } from '../../test-utils';
import api from '../../services/api';
import { RoutingImportWizard } from './RoutingImportWizard';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    previewRoutingImport: jest.fn(),
    commitRoutingImport: jest.fn(),
    downloadImportTemplate: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const previewResponse = {
  dry_run: true,
  total_rows: 5,
  parts_detected: 2,
  routings_created: 2,
  total_operations: 4,
  skipped_count: 1,
  created_ids: [],
  results: [
    {
      rows: [2, 3],
      part_number: '818-3928-638',
      routing_revision: 'A',
      routing_id: null,
      operation_count: 2,
      total_setup_hours: 0.5,
      total_run_hours_per_unit: 0.25,
      status: 'draft' as const,
    },
    {
      rows: [4, 5],
      part_number: '820-5052-010',
      routing_revision: 'B',
      routing_id: null,
      operation_count: 2,
      total_setup_hours: 1,
      total_run_hours_per_unit: 0.5,
      status: 'draft' as const,
    },
  ],
  errors: [{ row: 6, part_number: 'BAD-1', reason: 'work center DEBURR not found' }],
};

const commitResponse = {
  ...previewResponse,
  dry_run: false,
  created_ids: [101, 102],
  results: previewResponse.results.map((r, i) => ({ ...r, routing_id: 101 + i })),
};

function selectFile() {
  const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(fileInput, {
    target: { files: [new File(['routing'], 'routings.csv', { type: 'text/csv' })] },
  });
}

async function advanceToPreview(onComplete = jest.fn().mockResolvedValue(undefined)) {
  mockedApi.previewRoutingImport.mockResolvedValue(previewResponse);
  mockedApi.commitRoutingImport.mockResolvedValue(commitResponse);

  renderWithRouter(<RoutingImportWizard onComplete={onComplete} onClose={jest.fn()} />);

  selectFile();
  fireEvent.submit(document.getElementById('routing-import-form') as HTMLFormElement);

  await screen.findByText('Review Routing Import');
  return onComplete;
}

describe('RoutingImportWizard', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('renders the upload step with the column hint and a template download', () => {
    renderWithRouter(<RoutingImportWizard onComplete={jest.fn()} onClose={jest.fn()} />);

    expect(screen.getByText('Import Routings')).toBeInTheDocument();
    expect(document.getElementById('routing-import-form')).toBeInTheDocument();
    expect(document.querySelector('input[type="file"]')).toBeInTheDocument();
    // Required-column hint is surfaced for users hand-building a CSV.
    expect(screen.getByText(/Required: part_number, sequence, operation_name, work_center_code/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /download template/i })).toBeInTheDocument();
    // Preview is disabled until a file is chosen.
    expect(screen.getByRole('button', { name: /preview \(dry run\)/i })).toBeDisabled();
  });

  it('downloads the routings template from the server endpoint', async () => {
    (URL as any).createObjectURL = jest.fn(() => 'blob:mock');
    (URL as any).revokeObjectURL = jest.fn();
    jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    mockedApi.downloadImportTemplate.mockResolvedValue({
      blob: new Blob(['xlsx']),
      filename: 'werco-import-template-routings.xlsx',
    });

    renderWithRouter(<RoutingImportWizard onComplete={jest.fn()} onClose={jest.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /download template/i }));

    await waitFor(() => expect(mockedApi.downloadImportTemplate).toHaveBeenCalledWith('routings'));
  });

  it('runs a dry-run preview and renders summary chips, results, and error rows', async () => {
    await advanceToPreview();

    expect(mockedApi.previewRoutingImport).toHaveBeenCalledTimes(1);
    // Dry-run, no write yet.
    expect(screen.getByText(/nothing has been written yet/i)).toBeInTheDocument();

    // Summary chips.
    expect(screen.getByText('Parts detected')).toBeInTheDocument();
    expect(screen.getByText('Routings to create')).toBeInTheDocument();

    // Results rows render each creatable routing.
    expect(screen.getByText('818-3928-638')).toBeInTheDocument();
    expect(screen.getByText('820-5052-010')).toBeInTheDocument();

    // Error row renders with row number, part, and reason; commit-skip warning shown.
    expect(screen.getByText('work center DEBURR not found')).toBeInTheDocument();
    expect(screen.getByText('BAD-1')).toBeInTheDocument();
    expect(screen.getByText(/their routings will be skipped on commit/i)).toBeInTheDocument();
  });

  it('commits the import and shows the success summary with a link to the Routing page', async () => {
    const onComplete = await advanceToPreview();

    fireEvent.click(screen.getByRole('button', { name: /commit 2 routings/i }));

    await screen.findByText('Routings Imported');
    expect(mockedApi.commitRoutingImport).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));

    expect(screen.getByText(/Created 2 routings/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /routing page/i })).toHaveAttribute('href', '/routing');
  });

  it('disables commit when the dry run would create nothing', async () => {
    mockedApi.previewRoutingImport.mockResolvedValue({
      ...previewResponse,
      parts_detected: 1,
      routings_created: 0,
      total_operations: 0,
      skipped_count: 1,
      results: [],
      errors: [{ row: 2, part_number: 'P-100', reason: 'part not found' }],
    });

    renderWithRouter(<RoutingImportWizard onComplete={jest.fn()} onClose={jest.fn()} />);
    selectFile();
    fireEvent.submit(document.getElementById('routing-import-form') as HTMLFormElement);

    await screen.findByText('Review Routing Import');
    expect(screen.getByText('part not found')).toBeInTheDocument();
    // Nothing creatable → commit blocked.
    expect(screen.getByRole('button', { name: /commit 0 routings/i })).toBeDisabled();
  });

  it('shows a permission message when the server returns 403', async () => {
    mockedApi.previewRoutingImport.mockRejectedValue({ response: { status: 403 } });

    renderWithRouter(<RoutingImportWizard onComplete={jest.fn()} onClose={jest.fn()} />);
    selectFile();
    fireEvent.submit(document.getElementById('routing-import-form') as HTMLFormElement);

    await screen.findByText(/you don't have permission to import routings/i);
    // Stays on the upload step — no preview rendered.
    expect(screen.queryByText('Review Routing Import')).not.toBeInTheDocument();
  });

  it('surfaces the backend 400 detail message verbatim', async () => {
    mockedApi.previewRoutingImport.mockRejectedValue({
      response: { status: 400, data: { detail: 'Unsupported file type' } },
    });

    renderWithRouter(<RoutingImportWizard onComplete={jest.fn()} onClose={jest.fn()} />);
    selectFile();
    fireEvent.submit(document.getElementById('routing-import-form') as HTMLFormElement);

    await screen.findByText('Unsupported file type');
  });
});
