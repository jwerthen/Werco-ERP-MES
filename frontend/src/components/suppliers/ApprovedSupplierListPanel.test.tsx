import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import api from '../../services/api';
import { ApprovedSupplierListPanel } from './ApprovedSupplierListPanel';
import type { AslVendor, VendorReevaluationDue } from '../../types/supplierApproval';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getVendors: jest.fn(),
    getVendorsReevaluationDue: jest.fn(),
    updateVendorApproval: jest.fn(),
    previewVendorAslImport: jest.fn(),
    commitVendorAslImport: jest.fn(),
    downloadImportTemplate: jest.fn(),
  },
}));
let mockRole = 'quality';
jest.mock('../../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, role: mockRole } }),
}));
const mockedApi = api as jest.Mocked<typeof api>;

const bramble: AslVendor = {
  id: 1,
  code: 'VND-001',
  name: 'Bramble Metals',
  is_approved: true,
  approval_status: 'approved',
  approved_scopes: ['raw_material_metallics'],
  approval_bases: ['as9100'],
  approval_date: '2023-05-01',
  reevaluation_due: '2025-05-01',
  last_performance_evaluation: '2024-05-01',
  approval_notes: null,
  updated_at: '2026-11-01T00:00:00Z',
};
const cinder: AslVendor = {
  id: 2,
  code: 'VND-002',
  name: 'Cinder-Forge',
  is_approved: false,
  approval_status: 'conditional',
  approved_scopes: ['hardware'],
  approval_bases: ['questionnaire'],
  reevaluation_due: '2024-12-10',
};
const planned: AslVendor = {
  id: 3,
  code: 'VND-003',
  name: 'Planned Supplier',
  is_approved: false,
  approval_status: null,
};

const overdue: VendorReevaluationDue[] = [
  { ...cinder, days_overdue: 703 },
  { ...bramble, days_overdue: 561 },
];

/** The desktop table (DataTable also renders mobile cards with the same content). */
const desktopTable = async (text: string) => {
  await waitFor(() => expect(within(screen.getByTestId('data-table')).getByText(text)).toBeInTheDocument());
  return screen.getByTestId('data-table');
};

const renderPanel = (filter: 'all' | 'on' | 'off' | 'overdue' = 'all', onFilterChange = jest.fn()) => {
  render(<ApprovedSupplierListPanel search="" filter={filter} onFilterChange={onFilterChange} />);
  return onFilterChange;
};

beforeEach(() => {
  jest.clearAllMocks();
  mockRole = 'quality';
  mockedApi.getVendors.mockResolvedValue([bramble, cinder, planned]);
  mockedApi.getVendorsReevaluationDue.mockResolvedValue(overdue);
});

describe('ApprovedSupplierListPanel', () => {
  it('lists every vendor with its ASL status, scope and basis from the vendor record', async () => {
    renderPanel();
    const table = await desktopTable('Bramble Metals');
    const brambleRow = within(table).getByText('Bramble Metals').closest('tr') as HTMLElement;
    expect(within(brambleRow).getByText('Approved')).toBeInTheDocument();
    expect(within(brambleRow).getByText('Raw Material - Metallics')).toBeInTheDocument();
    expect(within(brambleRow).getByText('AS9100 Cert')).toBeInTheDocument();
    const plannedRow = within(table).getByText('Planned Supplier').closest('tr') as HTMLElement;
    expect(within(plannedRow).getByText('Not on the ASL')).toBeInTheDocument();
    // The legacy approved_supplier_list table is no longer read.
    expect(mockedApi.getVendors).toHaveBeenCalledWith();
  });

  it('counts the overdue chip from the re-evaluation endpoint and switches the filter', async () => {
    const onFilterChange = renderPanel();
    const chip = await screen.findByRole('button', { name: /Re-evaluation overdue\s*2/ });
    expect(chip).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByRole('button', { name: /On the ASL\s*2/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Not on the ASL\s*1/ })).toBeInTheDocument();
    fireEvent.click(chip);
    expect(onFilterChange).toHaveBeenCalledWith('overdue');
  });

  it('shows the server’s overdue rows, with days overdue, under the overdue filter', async () => {
    renderPanel('overdue');
    const table = await desktopTable('Cinder-Forge');
    expect(within(table).queryByText('Planned Supplier')).not.toBeInTheDocument();
    expect(within(table).getByText('(703 d overdue)')).toBeInTheDocument();
  });

  it('offers Update approval and Import ASL to quality, and hides them from a supervisor', async () => {
    renderPanel();
    const table = await desktopTable('Bramble Metals');
    expect(within(table).getByRole('button', { name: 'Update approval for Bramble Metals' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Import ASL/ })).toBeInTheDocument();
  });

  it('is read-only for roles that cannot write the ASL', async () => {
    mockRole = 'supervisor';
    renderPanel();
    await desktopTable('Bramble Metals');
    expect(screen.queryByRole('button', { name: /Update approval/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Import ASL/ })).not.toBeInTheDocument();
  });

  it('reflects only the server’s saved vendor after an approval update', async () => {
    mockedApi.updateVendorApproval.mockResolvedValue({ ...planned, approval_status: 'approved', is_approved: true });
    renderPanel();
    const table = await desktopTable('Planned Supplier');
    fireEvent.click(within(table).getByRole('button', { name: 'Update approval for Planned Supplier' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('ASL status'), { target: { value: 'approved' } });
    fireEvent.change(within(dialog).getByLabelText(/Reason for this change/), { target: { value: 'Added to ASL' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save approval' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    const row = within(screen.getByTestId('data-table')).getByText('Planned Supplier').closest('tr') as HTMLElement;
    await waitFor(() => expect(within(row).getByText('Approved')).toBeInTheDocument());
    // The overdue list is re-read after a decision (its membership follows the status).
    expect(mockedApi.getVendorsReevaluationDue).toHaveBeenCalledTimes(2);
  });
});
