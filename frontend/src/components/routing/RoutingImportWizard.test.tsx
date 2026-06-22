import React from 'react';
import { fireEvent, renderWithRouter, screen, waitFor, within } from '../../test-utils';
import api from '../../services/api';
import { RoutingImportWizard } from './RoutingImportWizard';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    previewRoutingImport: jest.fn(),
    commitRoutingImport: jest.fn(),
    downloadImportTemplate: jest.fn(),
    getWorkCenters: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const workCenters = [
  { id: 5, code: 'WELD-A', name: 'Weld Bay A' },
  { id: 7, code: 'INSP-1', name: 'Inspection Cell' },
  { id: 9, code: 'CNC-3', name: 'CNC Mill 3' },
] as any;

// First routing's ops both come in blank (needs_work_center). Second routing's
// ops arrive pre-coded from the file (work_center_id non-null).
const previewResponse = {
  dry_run: true,
  total_rows: 5,
  parts_detected: 2,
  routings_created: 2,
  total_operations: 4,
  skipped_count: 1,
  operations_needing_work_center: 2,
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
      operations: [
        {
          row: 2,
          sequence: 10,
          operation_name: 'Saw Cut',
          work_center_code: null,
          work_center_id: null,
          work_center_name: null,
          needs_work_center: true,
          setup_hours: 0.25,
          run_hours_per_unit: 0.1,
          is_inspection_point: false,
          is_outside_operation: false,
        },
        {
          row: 3,
          sequence: 20,
          operation_name: 'Deburr',
          work_center_code: null,
          work_center_id: null,
          work_center_name: null,
          needs_work_center: true,
          setup_hours: 0.25,
          run_hours_per_unit: 0.15,
          is_inspection_point: false,
          is_outside_operation: false,
        },
      ],
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
      operations: [
        {
          row: 4,
          sequence: 10,
          operation_name: 'CNC Mill',
          work_center_code: 'CNC-3',
          work_center_id: 9,
          work_center_name: 'CNC Mill 3',
          needs_work_center: false,
          setup_hours: 0.5,
          run_hours_per_unit: 0.3,
          is_inspection_point: false,
          is_outside_operation: false,
        },
        {
          row: 5,
          sequence: 20,
          operation_name: 'Final Inspect',
          work_center_code: 'INSP-1',
          work_center_id: 7,
          work_center_name: 'Inspection Cell',
          needs_work_center: false,
          setup_hours: 0.5,
          run_hours_per_unit: 0.2,
          is_inspection_point: true,
          is_outside_operation: false,
        },
      ],
    },
  ],
  errors: [{ row: 6, part_number: 'BAD-1', reason: 'part BAD-1 not found' }],
};

const commitResponse = {
  ...previewResponse,
  dry_run: false,
  operations_needing_work_center: 0,
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
  mockedApi.getWorkCenters.mockResolvedValue(workCenters);

  renderWithRouter(<RoutingImportWizard onComplete={onComplete} onClose={jest.fn()} />);

  // Work centers are loaded on mount for the assignment dropdowns.
  await waitFor(() => expect(mockedApi.getWorkCenters).toHaveBeenCalled());

  selectFile();
  fireEvent.submit(document.getElementById('routing-import-form') as HTMLFormElement);

  await screen.findByText('Assign Work Centers');
  return onComplete;
}

/** Find the work-center <select> for a given operation by its aria-label row tag. */
function opSelect(operationName: string, row: number): HTMLSelectElement {
  return screen.getByLabelText(
    new RegExp(`Work center for operation ${operationName} \\(row ${row}\\)`, 'i'),
  ) as HTMLSelectElement;
}

describe('RoutingImportWizard', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getWorkCenters.mockResolvedValue(workCenters);
  });

  it('renders the upload step with the column hint and a template download', async () => {
    renderWithRouter(<RoutingImportWizard onComplete={jest.fn()} onClose={jest.fn()} />);
    // Flush the mount-time work-centers fetch so its state update is settled.
    await waitFor(() => expect(mockedApi.getWorkCenters).toHaveBeenCalled());

    expect(screen.getByText('Import Routings')).toBeInTheDocument();
    expect(document.getElementById('routing-import-form')).toBeInTheDocument();
    expect(document.querySelector('input[type="file"]')).toBeInTheDocument();
    // Required-column hint — work_center_code is now optional (assigned post-upload).
    expect(screen.getByText(/Required: part_number, sequence, operation_name/i)).toBeInTheDocument();
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

  it('runs a dry-run preview and renders the per-operation assignment table', async () => {
    await advanceToPreview();

    expect(mockedApi.previewRoutingImport).toHaveBeenCalledTimes(1);

    // Summary chips still render.
    expect(screen.getByText('Parts detected')).toBeInTheDocument();
    expect(screen.getByText('Routings to create')).toBeInTheDocument();

    // Each routing card renders with its part number and per-operation rows.
    expect(screen.getByText('818-3928-638')).toBeInTheDocument();
    expect(screen.getByText('820-5052-010')).toBeInTheDocument();
    expect(screen.getByText('Saw Cut')).toBeInTheDocument();
    expect(screen.getByText('Deburr')).toBeInTheDocument();
    expect(screen.getByText('CNC Mill')).toBeInTheDocument();
    expect(screen.getByText('Final Inspect')).toBeInTheDocument();

    // A work-center dropdown renders per operation, with code — name options.
    const sawSelect = opSelect('Saw Cut', 2);
    expect(within(sawSelect).getByRole('option', { name: 'WELD-A — Weld Bay A' })).toBeInTheDocument();

    // Error row still surfaces.
    expect(screen.getByText('part BAD-1 not found')).toBeInTheDocument();
  });

  it('pre-selects the dropdown for operations that arrived coded from the file', async () => {
    await advanceToPreview();

    // Row 4 came in with work_center_id 9 → its select is pre-filled.
    expect(opSelect('CNC Mill', 4).value).toBe('9');
    expect(opSelect('Final Inspect', 5).value).toBe('7');
    // Blank rows start empty.
    expect(opSelect('Saw Cut', 2).value).toBe('');
    expect(opSelect('Deburr', 3).value).toBe('');
  });

  it('keeps commit disabled until every operation has a work center', async () => {
    await advanceToPreview();

    // Two blank ops → commit disabled, count surfaced live (banner asserts the
    // count sourced from selection state). "still need a work center" appears in
    // both the banner and the footer, so scope to the banner testid.
    expect(screen.getByRole('button', { name: /commit 2 routings/i })).toBeDisabled();
    expect(within(screen.getByTestId('needs-assignment-banner')).getByText(/2 operations still need/i)).toBeInTheDocument();

    // Assign one of the two blanks → still disabled, count drops to 1.
    fireEvent.change(opSelect('Saw Cut', 2), { target: { value: '5' } });
    expect(screen.getByRole('button', { name: /commit 2 routings/i })).toBeDisabled();
    expect(within(screen.getByTestId('needs-assignment-banner')).getByText(/1 operation still needs/i)).toBeInTheDocument();

    // Assign the last blank → commit enabled.
    fireEvent.change(opSelect('Deburr', 3), { target: { value: '7' } });
    expect(screen.getByRole('button', { name: /commit 2 routings/i })).toBeEnabled();
  });

  it('apply-to-all assigns every operation in a routing and enables commit', async () => {
    await advanceToPreview();

    // Use the first routing's apply-to-all control to set all of its ops at once.
    const applySelect = screen.getByLabelText(
      /Apply work center to all operations of 818-3928-638/i,
    ) as HTMLSelectElement;
    fireEvent.change(applySelect, { target: { value: '5' } });
    fireEvent.click(screen.getAllByRole('button', { name: /^All$/i })[0]);

    // Both previously blank ops now carry WELD-A.
    expect(opSelect('Saw Cut', 2).value).toBe('5');
    expect(opSelect('Deburr', 3).value).toBe('5');

    // Everything assigned → commit enabled.
    expect(screen.getByRole('button', { name: /commit 2 routings/i })).toBeEnabled();
  });

  it('commits with the correct assignments JSON map and shows the success summary', async () => {
    const onComplete = await advanceToPreview();

    // Assign the two blank ops; leave the file-coded ones as-is.
    fireEvent.change(opSelect('Saw Cut', 2), { target: { value: '5' } });
    fireEvent.change(opSelect('Deburr', 3), { target: { value: '5' } });

    fireEvent.click(screen.getByRole('button', { name: /commit 2 routings/i }));

    await screen.findByText('Routings Imported');
    expect(mockedApi.commitRoutingImport).toHaveBeenCalledTimes(1);

    // The commit FormData carries file + the assignments map: every op row → wc id.
    const formData = mockedApi.commitRoutingImport.mock.calls[0][0] as FormData;
    expect(formData.get('file')).toBeInstanceOf(File);
    const sentAssignments = JSON.parse(formData.get('assignments') as string);
    expect(sentAssignments).toEqual({ '2': 5, '3': 5, '4': 9, '5': 7 });

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
      operations_needing_work_center: 0,
      results: [],
      errors: [{ row: 2, part_number: 'P-100', reason: 'part not found' }],
    });
    mockedApi.getWorkCenters.mockResolvedValue(workCenters);

    renderWithRouter(<RoutingImportWizard onComplete={jest.fn()} onClose={jest.fn()} />);
    selectFile();
    fireEvent.submit(document.getElementById('routing-import-form') as HTMLFormElement);

    await screen.findByText('Assign Work Centers');
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
    // Stays on the upload step — no assignment step rendered.
    expect(screen.queryByText('Assign Work Centers')).not.toBeInTheDocument();
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

  it('surfaces a commit-time errors[] rejection on the success/summary step', async () => {
    const onComplete = await advanceToPreview();
    // Backend still rejects a routing (e.g. an op slipped through with no WC).
    mockedApi.commitRoutingImport.mockResolvedValue({
      ...commitResponse,
      routings_created: 1,
      created_ids: [101],
      errors: [{ row: 3, part_number: '818-3928-638', reason: 'operation 20 has no work center' }],
    });

    fireEvent.change(opSelect('Saw Cut', 2), { target: { value: '5' } });
    fireEvent.change(opSelect('Deburr', 3), { target: { value: '5' } });
    fireEvent.click(screen.getByRole('button', { name: /commit 2 routings/i }));

    await screen.findByText('Routings Imported');
    expect(screen.getByText('operation 20 has no work center')).toBeInTheDocument();
    await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));
  });
});
