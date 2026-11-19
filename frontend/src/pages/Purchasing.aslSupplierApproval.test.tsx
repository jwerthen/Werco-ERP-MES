/**
 * Purchasing × Approved Supplier List (AS9100D 8.4).
 *
 *  - Create PO runs the pure-read supplier-approval check for the chosen vendor + line parts
 *    and shows its advisory warnings amber; a Disqualified vendor is refused (red, Create PO
 *    disabled) — the same text every PO-create door answers 409 with.
 *  - A created PO that carries ASL warnings is reported as a WARNING toast, not a clean success.
 *  - Vendors → Edit: while a vendor has an ASL status its Approved checkbox is locked (it
 *    follows the status) and the PUT does not send `is_approved` at all, so it can never
 *    contradict the status the server derives it from.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import Purchasing from './Purchasing';
import type { Part } from '../types';
import type { SupplierApprovalCheck } from '../types/supplierApproval';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getVendors: jest.fn(),
    getPurchaseOrders: jest.fn(),
    getParts: jest.fn(),
    createPurchaseOrder: jest.fn(),
    checkSupplierApproval: jest.fn(),
    getDocuments: jest.fn(),
    getDocumentTypes: jest.fn(),
    updateVendor: jest.fn(),
    // Quality flow-down clauses for the Create PO checklist (read when the modal opens).
    getPOQualityClauses: jest.fn(),
  },
}));
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, role: 'manager' }, isAuthenticated: true, isLoading: false }),
}));
const mockedApi = api as jest.Mocked<typeof api>;

const vendors = [
  {
    id: 1,
    code: 'VND-001',
    name: 'Bramble Metals',
    is_approved: true,
    is_as9100_certified: true,
    is_iso9001_certified: false,
    is_active: true,
    version: 0,
    approval_status: 'approved',
    reevaluation_due: '2025-05-01',
  },
  {
    id: 2,
    code: 'VND-002',
    name: 'Legacy Supply',
    is_approved: true,
    is_as9100_certified: false,
    is_iso9001_certified: false,
    is_active: true,
    version: 0,
    approval_status: null,
  },
  {
    id: 3,
    code: 'VND-003',
    name: 'Probation Plating',
    is_approved: false,
    is_as9100_certified: false,
    is_iso9001_certified: false,
    is_active: true,
    version: 0,
    approval_status: 'conditional',
  },
  {
    id: 4,
    code: 'VND-004',
    name: 'Banned Castings',
    is_approved: false,
    is_as9100_certified: false,
    is_iso9001_certified: false,
    is_active: true,
    version: 0,
    approval_status: 'disqualified',
  },
  {
    id: 5,
    code: 'VND-005',
    name: 'Unvetted Supply',
    is_approved: false,
    is_as9100_certified: false,
    is_iso9001_certified: false,
    is_active: true,
    version: 0,
    approval_status: null,
  },
];
const parts = [{ id: 9, part_number: 'HW-9', name: 'Rivet' }] as Part[];
const purchaseOrders = [
  {
    id: 10,
    po_number: 'PO-1001',
    vendor_id: 1,
    vendor_name: 'Bramble Metals',
    status: 'draft',
    order_date: '2026-06-20',
    required_date: '2026-07-01',
    total: 100,
    line_count: 1,
  },
];

// Typed against the real response contract, so a mock that drifts from what the server
// returns is a compile error (tsconfig.test.json), not a passing test.
const check = (overrides: Partial<SupplierApprovalCheck> = {}): SupplierApprovalCheck => ({
  vendor_id: 1,
  vendor_name: 'Bramble Metals',
  approval_status: 'approved',
  is_approved: true,
  reevaluation_due: '2025-05-01',
  refused: false,
  refusal: null,
  warnings: [
    {
      code: 'reevaluation_overdue',
      message: "Bramble Metals's supplier re-evaluation was due 2025-05-01.",
      part_id: null,
      part_number: null,
    },
  ],
  ...overrides,
});

const renderPurchasing = () =>
  render(
    <MemoryRouter initialEntries={['/purchasing']}>
      <ToastProvider>
        <Purchasing />
      </ToastProvider>
    </MemoryRouter>
  );

async function openCreatePO() {
  renderPurchasing();
  await screen.findByRole('heading', { name: 'Purchasing' });
  fireEvent.click(screen.getByRole('button', { name: /new po/i }));
  await screen.findByRole('heading', { name: 'Create Purchase Order' });
  return screen.getByLabelText(/vendor/i).closest('form') as HTMLFormElement;
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getVendors.mockResolvedValue(vendors);
  mockedApi.getPurchaseOrders.mockResolvedValue(purchaseOrders);
  mockedApi.getParts.mockResolvedValue(parts);
  mockedApi.getDocuments.mockResolvedValue([]);
  mockedApi.getDocumentTypes.mockResolvedValue([]);
  mockedApi.updateVendor.mockResolvedValue({});
  mockedApi.checkSupplierApproval.mockResolvedValue(check());
  mockedApi.createPurchaseOrder.mockResolvedValue({ id: 99, supplier_approval_warnings: [] });
  mockedApi.getPOQualityClauses.mockResolvedValue([]);
});

describe('Create PO — Approved Supplier List', () => {
  test('offers Conditional suppliers (a warning, not a gate) but never Disqualified or unapproved ones', async () => {
    const form = await openCreatePO();
    const picker = within(form).getByLabelText(/vendor/i) as HTMLSelectElement;
    const offered = Array.from(picker.options).map(option => option.textContent);
    expect(offered).toEqual([
      'Select vendor...',
      'VND-001 - Bramble Metals',
      'VND-002 - Legacy Supply',
      'VND-003 - Probation Plating (Conditional)',
    ]);
  });

  test('checks the vendor + line parts and shows the advisory warnings amber', async () => {
    const form = await openCreatePO();
    fireEvent.change(screen.getByLabelText(/vendor/i), { target: { value: '1' } });
    expect(await screen.findByText("Bramble Metals's supplier re-evaluation was due 2025-05-01.")).toBeInTheDocument();
    expect(mockedApi.checkSupplierApproval).toHaveBeenLastCalledWith({ vendor_id: 1, part_ids: [] });

    fireEvent.click(within(form).getByRole('button', { name: '+ Add Line' }));
    const partSelect = within(form).getAllByRole('combobox')[1];
    fireEvent.change(partSelect, { target: { value: '9' } });
    await waitFor(() =>
      expect(mockedApi.checkSupplierApproval).toHaveBeenLastCalledWith({ vendor_id: 1, part_ids: [9] })
    );
    // Advisory only: the order can still be raised.
    expect(within(form).getByRole('button', { name: 'Create PO' })).toBeEnabled();
  });

  test('a Disqualified vendor is refused before the click, with the server’s own text', async () => {
    const refusal =
      'Bramble Metals is Disqualified on the Approved Supplier List; a purchase order cannot be raised to it.';
    mockedApi.checkSupplierApproval.mockResolvedValue(
      check({
        approval_status: 'disqualified',
        is_approved: false,
        refused: true,
        refusal,
        warnings: [{ code: 'vendor_disqualified', message: refusal }],
      })
    );
    const form = await openCreatePO();
    fireEvent.change(screen.getByLabelText(/vendor/i), { target: { value: '1' } });
    const alert = await screen.findByText(refusal);
    expect(alert.closest('[role="alert"]')).not.toBeNull();
    // The matching warning is folded into the refusal, not repeated amber.
    expect(screen.getAllByText(refusal)).toHaveLength(1);
    expect(within(form).getByRole('button', { name: 'Create PO' })).toBeDisabled();
  });

  test('a created PO carrying ASL warnings is reported as a warning, not a clean success', async () => {
    mockedApi.createPurchaseOrder.mockResolvedValue({
      id: 99,
      supplier_approval_warnings: [
        { code: 'scope_not_approved', message: 'Bramble Metals is not approved for Hardware (part HW-9).', part_id: 9 },
      ],
    });
    const form = await openCreatePO();
    fireEvent.change(screen.getByLabelText(/vendor/i), { target: { value: '1' } });
    fireEvent.click(within(form).getByRole('button', { name: '+ Add Line' }));
    fireEvent.change(within(form).getAllByRole('combobox')[1], { target: { value: '9' } });
    fireEvent.change(within(form).getByLabelText('Unit price'), { target: { value: '2.5' } });
    fireEvent.submit(form);
    expect(
      await screen.findByText(
        /Purchase order created with 1 Approved Supplier List warning: Bramble Metals is not approved/
      )
    ).toBeInTheDocument();
    expect(screen.queryByText('Purchase order created')).not.toBeInTheDocument();
  });

  test('surfaces the server’s 409 when the vendor was disqualified after the page loaded', async () => {
    mockedApi.checkSupplierApproval.mockResolvedValue(check({ warnings: [] }));
    const refusal =
      'Bramble Metals is Disqualified on the Approved Supplier List; a purchase order cannot be raised to it.';
    mockedApi.createPurchaseOrder.mockRejectedValue({ response: { status: 409, data: { detail: refusal } } });
    const form = await openCreatePO();
    fireEvent.change(screen.getByLabelText(/vendor/i), { target: { value: '1' } });
    fireEvent.click(within(form).getByRole('button', { name: '+ Add Line' }));
    fireEvent.change(within(form).getAllByRole('combobox')[1], { target: { value: '9' } });
    fireEvent.change(within(form).getByLabelText('Unit price'), { target: { value: '2.5' } });
    fireEvent.submit(form);
    expect((await screen.findAllByText(refusal)).length).toBeGreaterThan(0);
    expect(screen.getByRole('heading', { name: 'Create Purchase Order' })).toBeInTheDocument();
  });
});

describe('Vendors — ASL status column and the locked Approved flag', () => {
  async function openEdit(name: string) {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing' });
    fireEvent.click(screen.getByRole('tab', { name: /^Vendors/i }));
    const row = (await screen.findByText(name)).closest('tr') as HTMLElement;
    return row;
  }

  test('shows the ASL status and locks Approved while a status is set (PUT omits is_approved)', async () => {
    const row = await openEdit('Bramble Metals');
    expect(within(row).getByText('Approved', { selector: 'span' })).toBeInTheDocument();
    fireEvent.click(within(row).getByRole('button', { name: 'Edit' }));
    await screen.findByRole('heading', { name: 'Edit Vendor' });
    const approved = screen.getByLabelText('Approved Vendor');
    expect(approved).toBeDisabled();
    expect(screen.getByText(/Follows the Approved Supplier List status/)).toBeInTheDocument();

    fireEvent.submit(approved.closest('form') as HTMLFormElement);
    await waitFor(() => expect(mockedApi.updateVendor).toHaveBeenCalledTimes(1));
    expect(mockedApi.updateVendor.mock.calls[0][1]).not.toHaveProperty('is_approved');
  });

  test('a vendor not on the ASL keeps the editable Approved flag', async () => {
    const row = await openEdit('Legacy Supply');
    expect(within(row).getByText('Not on the ASL')).toBeInTheDocument();
    fireEvent.click(within(row).getByRole('button', { name: 'Edit' }));
    await screen.findByRole('heading', { name: 'Edit Vendor' });
    const approved = screen.getByLabelText('Approved Vendor');
    expect(approved).toBeEnabled();
    fireEvent.submit(approved.closest('form') as HTMLFormElement);
    await waitFor(() => expect(mockedApi.updateVendor).toHaveBeenCalledTimes(1));
    expect(mockedApi.updateVendor.mock.calls[0][1]).toHaveProperty('is_approved', true);
  });
});
