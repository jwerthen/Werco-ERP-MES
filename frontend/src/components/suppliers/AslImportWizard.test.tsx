import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import api from '../../services/api';
import { AslImportWizard, formatAslChangeValue } from './AslImportWizard';
import type { AslImportResponse } from '../../types/supplierApproval';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    previewVendorAslImport: jest.fn(),
    commitVendorAslImport: jest.fn(),
    downloadImportTemplate: jest.fn(),
  },
}));
const mockedApi = api as jest.Mocked<typeof api>;

const HASH = 'a'.repeat(64);
const PLAN = 'b'.repeat(64);
const file = new File(['xlsx'], 'QL 7.4 - Approved Supplier List.xlsx');

const preview = (overrides: Partial<AslImportResponse['summary']> = {}): AslImportResponse => ({
  filename: file.name,
  source_hash: HASH,
  plan_hash: PLAN,
  document: { form_number: 'QL 7.4', revision: 'A', rev_date: '2026-01-30' },
  committed: false,
  summary: {
    total_rows: 3,
    matched_rows: 2,
    update_count: 1,
    unchanged_count: 1,
    error_count: 0,
    warning_count: 1,
    not_on_asl_count: 0,
    ...overrides,
  },
  rows: [
    {
      row: 6,
      supplier_name: 'Bramble Metals',
      vendor_id: 1,
      vendor_code: 'VND-001',
      vendor_name: 'Bramble Metals',
      match_by: 'name',
      outcome: 'update',
      changes: {
        approval_status: { old: null, new: 'approved' },
        approved_scopes: { old: null, new: ['raw_material_metallics'] },
        is_approved: { old: false, new: true },
      },
      warnings: ['SUPPLIER ADDRESS differs from the vendor record; this import never changes an address.'],
    },
    {
      row: 7,
      supplier_name: 'Tejas Alloys',
      vendor_id: 2,
      vendor_code: 'VND-002',
      vendor_name: 'Tejas Alloys',
      match_by: 'name',
      outcome: 'unchanged',
      changes: {},
      warnings: [],
    },
  ],
});

const upload = async () => {
  fireEvent.change(screen.getByLabelText(/File \(\.xlsx or \.csv\)/), { target: { files: [file] } });
  fireEvent.click(screen.getByRole('button', { name: /Preview \(dry run\)/ }));
  await screen.findByRole('heading', { name: 'Review ASL changes' });
};

beforeEach(() => jest.clearAllMocks());

describe('AslImportWizard', () => {
  it('formats change values in the ASL screen’s own words', () => {
    expect(formatAslChangeValue('approval_status', 'approved_customer_directed')).toBe('Approved - Customer Directed');
    expect(formatAslChangeValue('approved_scopes', ['ndt_inspection_laboratory'])).toBe(
      'NDT / Inspection / Laboratory Services'
    );
    expect(formatAslChangeValue('approval_bases', ['iso9001'])).toBe('ISO 9001 Cert');
    expect(formatAslChangeValue('is_approved', true)).toBe('Yes');
    expect(formatAslChangeValue('reevaluation_due', null)).toBe('—');
  });

  it('previews the diff, hides unchanged rows, then commits the same file with the preview hash', async () => {
    mockedApi.previewVendorAslImport.mockResolvedValue(preview());
    mockedApi.commitVendorAslImport.mockResolvedValue({ ...preview(), committed: true });
    const onCommitted = jest.fn();
    render(<AslImportWizard onClose={jest.fn()} onCommitted={onCommitted} />);
    await upload();

    expect(mockedApi.previewVendorAslImport).toHaveBeenCalledWith(file);
    const table = screen.getByRole('table');
    // The sheet's supplier cell is the row's first name cell (the matched vendor repeats it).
    const bramble = within(table).getAllByText('Bramble Metals', { selector: 'td' })[0].closest('tr') as HTMLElement;
    expect(within(bramble).getByText('Raw Material - Metallics')).toBeInTheDocument();
    expect(within(bramble).getByText(/SUPPLIER ADDRESS differs/)).toBeInTheDocument();
    // Unchanged rows are folded away until asked for.
    expect(within(table).queryAllByText('Tejas Alloys', { selector: 'td' })).toHaveLength(0);
    fireEvent.click(screen.getByLabelText(/Show unchanged rows/));
    expect(within(table).getAllByText('Tejas Alloys', { selector: 'td' }).length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole('button', { name: /Commit 1 vendor update/ }));
    await screen.findByRole('heading', { name: 'Approved Supplier List imported' });
    expect(mockedApi.commitVendorAslImport).toHaveBeenCalledWith(file, HASH, PLAN);
    expect(onCommitted).toHaveBeenCalled();
  });

  it('cannot commit while any row has an error, or when nothing would change', async () => {
    mockedApi.previewVendorAslImport.mockResolvedValue(preview({ error_count: 1 }));
    const { unmount } = render(<AslImportWizard onClose={jest.fn()} onCommitted={jest.fn()} />);
    await upload();
    expect(screen.getByRole('button', { name: /Commit 1 vendor update/ })).toBeDisabled();
    unmount();

    mockedApi.previewVendorAslImport.mockResolvedValue(preview({ update_count: 0 }));
    render(<AslImportWizard onClose={jest.fn()} onCommitted={jest.fn()} />);
    await upload();
    expect(screen.getByText(/there is nothing to commit/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Commit 0 vendor updates/ })).toBeDisabled();
  });

  it('shows the all-or-nothing 409 row errors and writes nothing', async () => {
    mockedApi.previewVendorAslImport.mockResolvedValue(preview());
    mockedApi.commitVendorAslImport.mockRejectedValue({
      response: {
        status: 409,
        data: {
          detail: {
            message: '1 row(s) have errors; nothing was imported. Fix them and preview again.',
            errors: [{ row: 9, supplier_name: 'Cinder- Forge', error: 'No vendor named Cinder- Forge' }],
          },
        },
      },
    });
    const onCommitted = jest.fn();
    render(<AslImportWizard onClose={jest.fn()} onCommitted={onCommitted} />);
    await upload();
    fireEvent.click(screen.getByRole('button', { name: /Commit 1 vendor update/ }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('nothing was imported');
    expect(alert).toHaveTextContent('Row 9 (Cinder- Forge): No vendor named Cinder- Forge');
    await waitFor(() => expect(onCommitted).not.toHaveBeenCalled());
    expect(screen.getByRole('heading', { name: 'Review ASL changes' })).toBeInTheDocument();
  });
});
