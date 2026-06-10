/**
 * A0.2 Excel migration kit — Import Center preview-before-commit flow.
 *
 * Guards the heart of the migration kit: uploads always run dry_run=true
 * first, the results panel shows would-create / skipped / row-level errors,
 * and "Commit import" re-submits the same file with dry_run=false — and is
 * disabled when the dry run shows nothing would be created.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import ImportCenter from './ImportCenter';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getImportTemplates: jest.fn(),
    downloadImportTemplate: jest.fn(),
    importUsersCsv: jest.fn(),
    importPartsCsv: jest.fn(),
    importMaterialsCsv: jest.fn(),
    importCustomersCsv: jest.fn(),
    importVendorsCsv: jest.fn(),
    importWorkCentersCsv: jest.fn(),
    importWorkOrders: jest.fn(),
    importPurchaseOrders: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const xlsxFile = new File(['stub'], 'legacy.xlsx', {
  type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
});

function renderImportCenter(query = '') {
  return render(
    <MemoryRouter initialEntries={[`/import-center${query}`]}>
      <ImportCenter />
    </MemoryRouter>
  );
}

function chooseFile(file: File) {
  fireEvent.change(screen.getByLabelText('Import file'), { target: { files: [file] } });
}

function validateButton() {
  return screen.getByRole('button', { name: /validate file \(dry run\)/i });
}

beforeAll(() => {
  (URL as any).createObjectURL = jest.fn(() => 'blob:mock');
  (URL as any).revokeObjectURL = jest.fn();
  jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
});

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getImportTemplates.mockResolvedValue({
    templates: [
      {
        entity: 'parts',
        title: 'Engineering parts',
        description: 'Manufactured/assembly part master.',
        columns: ['part_number', 'name', 'part_type'],
        download_path: '/api/v1/import/templates/parts',
      },
      {
        entity: 'work-orders',
        title: 'Open work orders',
        description: 'Open (in-flight) work orders for go-live.',
        columns: ['wo_number', 'part_number', 'quantity', 'completed_through_seq'],
        download_path: '/api/v1/import/templates/work-orders',
      },
    ],
  });
});

describe('ImportCenter preview-before-commit flow', () => {
  it('runs dry_run=true, renders row-level errors, and disables commit when nothing would be created', async () => {
    mockedApi.importPartsCsv.mockResolvedValue({
      dry_run: true,
      total_rows: 2,
      imported_count: 0,
      skipped_count: 2,
      created_ids: [],
      errors: [
        { row: 2, part_number: 'P-100', reason: 'duplicate part_number' },
        { row: 3, reason: 'name is required' },
      ],
    });

    renderImportCenter('?type=parts');

    const input = screen.getByLabelText('Import file');
    expect(input.getAttribute('accept')).toContain('.xlsx');
    expect(input.getAttribute('accept')).toContain('.csv');

    chooseFile(xlsxFile);
    fireEvent.click(validateButton());

    await screen.findByText('Dry run preview');
    expect(mockedApi.importPartsCsv).toHaveBeenCalledTimes(1);
    expect(mockedApi.importPartsCsv).toHaveBeenCalledWith(xlsxFile, true);

    // Row-level errors render with row number, identifier, and reason.
    expect(screen.getByText('duplicate part_number')).toBeInTheDocument();
    expect(screen.getByText('P-100')).toBeInTheDocument();
    expect(screen.getByText('name is required')).toBeInTheDocument();

    // All-errors dry run: commit is blocked.
    expect(screen.getByRole('button', { name: /commit import/i })).toBeDisabled();
    expect(screen.getByText(/nothing would be created/i)).toBeInTheDocument();
  });

  it('commit re-submits the same file with dry_run=false and shows the committed panel', async () => {
    mockedApi.importPartsCsv
      .mockResolvedValueOnce({
        dry_run: true,
        total_rows: 2,
        imported_count: 2,
        skipped_count: 0,
        created_ids: [],
        errors: [],
      })
      .mockResolvedValueOnce({
        dry_run: false,
        total_rows: 2,
        imported_count: 2,
        skipped_count: 0,
        created_ids: [11, 12],
        errors: [],
      });

    renderImportCenter('?type=parts');
    chooseFile(xlsxFile);
    fireEvent.click(validateButton());

    await screen.findByText('Dry run preview');
    const commitButton = screen.getByRole('button', { name: /commit import/i });
    expect(commitButton).toBeEnabled();

    fireEvent.click(commitButton);
    await screen.findByText('Import committed');

    expect(mockedApi.importPartsCsv).toHaveBeenCalledTimes(2);
    expect(mockedApi.importPartsCsv).toHaveBeenLastCalledWith(xlsxFile, false);
    expect(screen.getByText(/created: 2/i)).toBeInTheDocument();
  });

  it('previews and commits open work orders, showing paper-history operation state per row', async () => {
    mockedApi.importWorkOrders
      .mockResolvedValueOnce({
        dry_run: true,
        total_rows: 2,
        created_count: 1,
        skipped_count: 1,
        created_ids: [],
        results: [
          {
            row: 2,
            wo_number: null,
            part_number: '1042-100',
            quantity: 25,
            due_date: '2026-07-15',
            customer_name: 'Acme Aero',
            status: 'in_progress',
            operation_count: 4,
            completed_operation_count: 2,
            next_operation_sequence: 30,
          },
        ],
        errors: [{ row: 3, part_number: 'BAD-1', reason: 'part not found' }],
      })
      .mockResolvedValueOnce({
        dry_run: false,
        total_rows: 2,
        created_count: 1,
        skipped_count: 1,
        created_ids: [501],
        results: [
          {
            row: 2,
            wo_number: 'WO-2026-0100',
            part_number: '1042-100',
            quantity: 25,
            due_date: '2026-07-15',
            customer_name: 'Acme Aero',
            status: 'in_progress',
            operation_count: 4,
            completed_operation_count: 2,
            next_operation_sequence: 30,
          },
        ],
        errors: [{ row: 3, part_number: 'BAD-1', reason: 'part not found' }],
      });

    renderImportCenter('?type=work_orders');

    // The hub section carries the plain-English explainer for nervous migrators.
    expect(screen.getByText(/paper-history/i)).toBeInTheDocument();

    chooseFile(xlsxFile);
    fireEvent.click(validateButton());

    await screen.findByText('Dry run preview');
    expect(mockedApi.importWorkOrders).toHaveBeenCalledWith(xlsxFile, true);

    // Preview row: generated number placeholder, ops complete, next ready op, and the error row.
    expect(screen.getByText('(generated at commit)')).toBeInTheDocument();
    expect(screen.getByText('2/4')).toBeInTheDocument();
    expect(screen.getByText('Seq 30')).toBeInTheDocument();
    expect(screen.getByText('part not found')).toBeInTheDocument();

    const commitButton = screen.getByRole('button', { name: /commit import/i });
    expect(commitButton).toBeEnabled();
    fireEvent.click(commitButton);

    await screen.findByText('Import committed');
    expect(mockedApi.importWorkOrders).toHaveBeenLastCalledWith(xlsxFile, false);
    expect(screen.getByText('WO-2026-0100')).toBeInTheDocument();
  });

  it('downloads the server XLSX template for the selected entity', async () => {
    mockedApi.downloadImportTemplate.mockResolvedValue({
      blob: new Blob(['xlsx-bytes']),
      filename: 'werco-import-template-parts.xlsx',
    });

    renderImportCenter('?type=parts');
    fireEvent.click(screen.getByRole('button', { name: /download template \(\.xlsx\)/i }));

    await waitFor(() => {
      expect(mockedApi.downloadImportTemplate).toHaveBeenCalledWith('parts');
    });
    expect(URL.createObjectURL).toHaveBeenCalled();
  });
});
