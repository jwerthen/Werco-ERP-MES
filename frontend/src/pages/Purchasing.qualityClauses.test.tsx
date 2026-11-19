/**
 * Purchasing → Create Purchase Order: the quality flow-down clause checklist (AS9100D 8.4.3).
 *
 *  - the active catalog is read when the modal opens (never on the default page load), and its
 *    default clauses arrive pre-checked;
 *  - the create sends the buyer's selection — the pre-checked defaults when untouched, the
 *    changed list otherwise, in catalog order;
 *  - when the catalog offers no active clause, or could not be read, `quality_clause_codes` is
 *    left OFF the request, so the server applies the catalog defaults rather than pinning the PO
 *    to "no clauses".
 *
 * The api module and AuthContext are mocked at their boundary, as in the sibling Purchasing
 * tests. Clause codes and wording are invented.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import Purchasing from './Purchasing';
import type { POQualityClause } from '../types/poFlowdown';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getVendors: jest.fn(),
    getPurchaseOrders: jest.fn(),
    getParts: jest.fn(),
    createPurchaseOrder: jest.fn(),
    getPOQualityClauses: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, role: 'manager' }, isAuthenticated: true, isLoading: false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const CATALOG: POQualityClause[] = [
  {
    id: 11,
    code: 'QX1',
    title: 'Alpha requirement',
    body: null,
    default_selected: true,
    is_active: true,
    sort_order: 20,
  },
  {
    id: 12,
    code: 'QX2',
    title: 'Bravo requirement',
    body: null,
    default_selected: false,
    is_active: true,
    sort_order: 10,
  },
  {
    id: 13,
    code: 'QX3',
    title: 'Charlie requirement',
    body: null,
    default_selected: true,
    is_active: true,
    sort_order: 10,
  },
];

const clauseBox = (code: string) => screen.getByRole('checkbox', { name: new RegExp(`^${code}\\b`) });

async function openCreatePOForm(): Promise<HTMLFormElement> {
  render(
    <MemoryRouter>
      <ToastProvider>
        <Purchasing />
      </ToastProvider>
    </MemoryRouter>
  );
  await screen.findByRole('heading', { name: 'Purchasing' });
  expect(mockedApi.getPOQualityClauses).not.toHaveBeenCalled(); // not on the default page load
  // The header button (an empty order list renders a second one in its empty state).
  fireEvent.click(screen.getAllByRole('button', { name: /new po/i })[0]);
  await screen.findByRole('heading', { name: 'Create Purchase Order' });
  return screen.getByLabelText(/vendor/i).closest('form') as HTMLFormElement;
}

/** Fill the minimum a create needs (vendor, one line with a part) and submit. */
async function submitValidPO(form: HTMLFormElement): Promise<Record<string, unknown>> {
  fireEvent.change(screen.getByLabelText(/vendor/i), { target: { value: '1' } });
  fireEvent.click(screen.getByRole('button', { name: /add line/i }));
  fireEvent.change(screen.getByDisplayValue('Select part...'), { target: { value: '9' } });
  fireEvent.submit(form);
  await waitFor(() => expect(mockedApi.createPurchaseOrder).toHaveBeenCalledTimes(1));
  return mockedApi.createPurchaseOrder.mock.calls[0][0] as Record<string, unknown>;
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getVendors.mockResolvedValue([
    { id: 1, code: 'VFD-1', name: 'Flowdown Test Vendor', is_approved: true, is_active: true, version: 0 },
  ] as any);
  mockedApi.getPurchaseOrders.mockResolvedValue([] as any);
  mockedApi.getParts.mockResolvedValue([{ id: 9, part_number: 'PFD-1', name: 'Test part' }] as any);
  mockedApi.createPurchaseOrder.mockResolvedValue({ id: 99 } as any);
  mockedApi.getPOQualityClauses.mockResolvedValue(CATALOG.map(clause => ({ ...clause })));
});

describe('Purchasing — create PO quality clauses', () => {
  test('the catalog defaults arrive pre-checked when the modal opens', async () => {
    await openCreatePOForm();

    await waitFor(() => expect(clauseBox('QX1')).toBeChecked());
    expect(mockedApi.getPOQualityClauses).toHaveBeenCalledTimes(1);
    expect(clauseBox('QX3')).toBeChecked();
    expect(clauseBox('QX2')).not.toBeChecked();
    expect(screen.getByText(/Frozen when the PO is issued/)).toBeInTheDocument();
  });

  test('an untouched checklist creates the PO with the pre-checked defaults', async () => {
    const form = await openCreatePOForm();
    await waitFor(() => expect(clauseBox('QX1')).toBeChecked());

    const payload = await submitValidPO(form);

    expect(payload.quality_clause_codes).toEqual(['QX3', 'QX1']);
  });

  test('a changed checklist creates the PO with that selection, in catalog order', async () => {
    const form = await openCreatePOForm();
    await waitFor(() => expect(clauseBox('QX1')).toBeChecked());

    fireEvent.click(clauseBox('QX1'));
    fireEvent.click(clauseBox('QX2'));
    expect(clauseBox('QX1')).not.toBeChecked();
    expect(clauseBox('QX2')).toBeChecked();
    const payload = await submitValidPO(form);

    expect(payload.quality_clause_codes).toEqual(['QX2', 'QX3']);
  });

  test('unchecking every clause creates the PO with an explicit empty list', async () => {
    const form = await openCreatePOForm();
    await waitFor(() => expect(clauseBox('QX1')).toBeChecked());

    fireEvent.click(clauseBox('QX1'));
    fireEvent.click(clauseBox('QX3'));
    const payload = await submitValidPO(form);

    expect(payload.quality_clause_codes).toEqual([]);
  });

  test('a catalog with no active clause leaves the field off the request', async () => {
    mockedApi.getPOQualityClauses.mockResolvedValue([]);
    const form = await openCreatePOForm();
    expect(await screen.findByText(/No quality clauses are set up/)).toBeInTheDocument();

    const payload = await submitValidPO(form);

    expect(payload).not.toHaveProperty('quality_clause_codes');
  });

  test('a catalog that could not be read says so, never blocks the create, and leaves the field off', async () => {
    mockedApi.getPOQualityClauses.mockRejectedValue(new Error('offline'));
    const form = await openCreatePOForm();
    expect(
      await screen.findByText(
        /Quality clauses could not be loaded\. The purchase order will carry the catalog's default clauses/
      )
    ).toBeInTheDocument();

    const payload = await submitValidPO(form);

    expect(payload).not.toHaveProperty('quality_clause_codes');
  });
});
