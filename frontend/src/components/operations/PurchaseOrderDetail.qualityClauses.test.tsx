/**
 * The quality flow-down clauses on the purchase-order detail / edit modal (AS9100D 8.4.3).
 *
 * A DRAFT shows the clause checklist: the PO's stored selection, or — when it stores none (NULL)
 * — the catalog's default clauses pre-checked, with a note that the draft prints the defaults.
 * `quality_clause_codes` goes out on Save draft ONLY when the selection changed (set comparison):
 * an untouched list leaves the stored value, NULL included, exactly as it was, and a catalog that
 * could not be read never turns into a request that rewrites the selection.
 *
 * An ISSUED PO shows, read-only, what was frozen onto it ("Frozen when the PO was issued.") from
 * `flowdown_snapshot` — never the stored codes, which can name a clause retired before issue.
 *
 * The api module is mocked at its boundary. Clause codes and wording are invented.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import PurchaseOrderDetail from './PurchaseOrderDetail';
import api from '../../services/api';
import type { POQualityClause } from '../../types/poFlowdown';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getPurchaseOrder: jest.fn(),
    updatePurchaseOrder: jest.fn(),
    updateSupplierConfirmation: jest.fn(),
    getPOQualityClauses: jest.fn(),
  },
}));
jest.mock('../../hooks/usePermissions', () => ({ __esModule: true, default: () => ({ can: () => true }) }));
jest.mock('./EntityPicker', () => ({ __esModule: true, default: ({ id }: { id: string }) => <input id={id} /> }));

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
    body: 'Charlie clause text.',
    default_selected: true,
    is_active: true,
    sort_order: 10,
  },
];

function purchaseOrder(overrides: Record<string, unknown> = {}) {
  return {
    id: 7,
    po_number: 'PO-FD-0007',
    status: 'draft',
    version: 0,
    updated_at: '2026-11-19T15:00:00Z',
    vendor: { code: 'VFD-1', name: 'Flowdown Test Vendor' },
    required_date: null,
    expected_date: null,
    ship_to: '',
    shipping_method: '',
    notes: '',
    supplier_approval_warnings: [],
    lines: [
      {
        id: 70,
        part_id: 9,
        quantity_ordered: 2,
        quantity_received: 0,
        unit_price: 3.5,
        required_date: null,
        notes: '',
        part: { part_number: 'PFD-1', name: 'Test part' },
      },
    ],
    quality_clause_codes: null,
    flowdown_frozen: false,
    flowdown_snapshot: null,
    ...overrides,
  };
}

function renderDetail() {
  render(
    <MemoryRouter>
      <PurchaseOrderDetail id={7} onClose={jest.fn()} onSaved={jest.fn()} onLoaded={jest.fn()} />
    </MemoryRouter>
  );
}

const clauseBox = (code: string) => screen.getByRole('checkbox', { name: new RegExp(`^${code}\\b`) });
const savedPayload = () => mockedApi.updatePurchaseOrder.mock.calls[0][1] as Record<string, unknown>;

async function saveDraft() {
  fireEvent.click(screen.getByRole('button', { name: 'Save draft' }));
  await waitFor(() => expect(mockedApi.updatePurchaseOrder).toHaveBeenCalledTimes(1));
}

beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.setItem('user', JSON.stringify({ id: 1, company_id: 1 }));
  mockedApi.getPurchaseOrder.mockResolvedValue(purchaseOrder() as any);
  mockedApi.getPOQualityClauses.mockResolvedValue(CATALOG.map(clause => ({ ...clause })));
  mockedApi.updatePurchaseOrder.mockResolvedValue(purchaseOrder() as any);
});
afterEach(() => jest.restoreAllMocks());

describe('PurchaseOrderDetail — draft clause checklist', () => {
  test('a draft with no choice shows the catalog defaults pre-checked, in catalog order', async () => {
    renderDetail();

    await waitFor(() => expect(clauseBox('QX1')).toBeChecked());
    expect(mockedApi.getPOQualityClauses).toHaveBeenCalled();
    expect(clauseBox('QX3')).toBeChecked();
    expect(clauseBox('QX2')).not.toBeChecked();
    expect(clauseBox('QX1')).toBeEnabled();
    expect(screen.getByText(/No clauses chosen yet: this draft prints the catalog defaults/)).toBeInTheDocument();
    const list = screen.getByTestId('po-quality-clauses');
    expect(
      within(list)
        .getAllByRole('checkbox')
        .map(box => box.closest('li')?.textContent?.slice(0, 3))
    ).toEqual(['QX2', 'QX3', 'QX1']);
  });

  test('an untouched selection is not sent, so the stored NULL stays NULL', async () => {
    renderDetail();
    await waitFor(() => expect(clauseBox('QX1')).toBeChecked());

    await saveDraft();

    expect(savedPayload()).not.toHaveProperty('quality_clause_codes');
  });

  test('a changed selection is sent in catalog order', async () => {
    renderDetail();
    await waitFor(() => expect(clauseBox('QX1')).toBeChecked());

    fireEvent.click(clauseBox('QX1')); // uncheck a default
    fireEvent.click(clauseBox('QX2')); // check a non-default
    await saveDraft();

    expect(savedPayload().quality_clause_codes).toEqual(['QX2', 'QX3']);
  });

  test('changing the selection and changing it back sends nothing', async () => {
    renderDetail();
    await waitFor(() => expect(clauseBox('QX1')).toBeChecked());

    fireEvent.click(clauseBox('QX2'));
    fireEvent.click(clauseBox('QX2'));
    await saveDraft();

    expect(savedPayload()).not.toHaveProperty('quality_clause_codes');
  });

  test('a stored selection is shown as stored, and unchecking everything sends an empty list', async () => {
    mockedApi.getPurchaseOrder.mockResolvedValue(purchaseOrder({ quality_clause_codes: ['QX2'] }) as any);
    renderDetail();
    await waitFor(() => expect(clauseBox('QX2')).toBeChecked());
    expect(clauseBox('QX1')).not.toBeChecked();
    expect(screen.queryByText(/No clauses chosen yet/)).toBeNull();

    fireEvent.click(clauseBox('QX2'));
    await saveDraft();

    expect(savedPayload().quality_clause_codes).toEqual([]);
  });

  test('a stored code the active catalog no longer offers stays visible and can be dropped', async () => {
    mockedApi.getPurchaseOrder.mockResolvedValue(purchaseOrder({ quality_clause_codes: ['QX3', 'QX8'] }) as any);
    renderDetail();
    await waitFor(() => expect(clauseBox('QX3')).toBeChecked());

    const retired = clauseBox('QX8');
    expect(retired).toBeChecked();
    expect(retired.closest('li')).toHaveTextContent('no longer in the active clause catalog');

    fireEvent.click(retired);
    await saveDraft();

    expect(savedPayload().quality_clause_codes).toEqual(['QX3']);
  });

  test('a catalog that could not be read says so and never sends a selection', async () => {
    mockedApi.getPOQualityClauses.mockRejectedValue(new Error('offline'));
    renderDetail();

    expect(
      await screen.findByText(
        /Quality clauses could not be loaded\. Saving leaves this purchase order's clause selection unchanged/
      )
    ).toBeInTheDocument();
    expect(screen.queryByRole('checkbox', { name: /^QX/ })).toBeNull();
    await saveDraft();

    expect(savedPayload()).not.toHaveProperty('quality_clause_codes');
  });
});

describe('PurchaseOrderDetail — an issued PO shows its frozen flow-down', () => {
  test('lists the clauses frozen at issue, read-only, not the stored codes', async () => {
    mockedApi.getPurchaseOrder.mockResolvedValue(
      purchaseOrder({
        status: 'sent',
        flowdown_frozen: true,
        // QX2 was retired before the send, so the freeze dropped it: the supplier never saw it.
        quality_clause_codes: ['QX1', 'QX2'],
        flowdown_snapshot: {
          enabled: true,
          heading: 'Supplier Quality Terms (test)',
          document_reference: 'SQR-TEST Rev B',
          body: 'First test paragraph.',
          clauses: [{ code: 'QX1', title: 'Alpha requirement', body: null }],
        },
      }) as any
    );
    renderDetail();

    const frozen = await screen.findByTestId('po-frozen-flowdown');
    expect(frozen).toHaveTextContent('Frozen when the PO was issued.');
    expect(frozen).toHaveTextContent('Supplier Quality Terms (test)');
    expect(frozen).toHaveTextContent('Reference: SQR-TEST Rev B');
    const printed = within(frozen).getByRole('list', { name: 'Clauses printed on this purchase order' });
    expect(
      within(printed)
        .getAllByRole('listitem')
        .map(item => item.textContent)
    ).toEqual(['QX1 — Alpha requirement']);
    expect(frozen).not.toHaveTextContent('QX2');
    // Nothing to tick and nothing to save.
    expect(screen.queryByRole('checkbox', { name: /^QX/ })).toBeNull();
    expect(screen.queryByTestId('po-quality-clauses')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Save draft' })).toBeNull();
    expect(mockedApi.updatePurchaseOrder).not.toHaveBeenCalled();
  });

  test.each([
    [
      'a block frozen with no clauses',
      { enabled: true, heading: 'Terms (test)', clauses: [] },
      'No clauses were printed with it.',
    ],
    ['flow-down disabled at issue', { enabled: false }, 'No quality flow-down block was printed on this PO.'],
    ['a PO issued before flow-down was recorded', null, 'Issued before quality flow-down was recorded.'],
    ['an API that does not report the snapshot', undefined, 'Open the printed purchase order to see the flow-down'],
  ])('%s', async (_label, snapshot, expected) => {
    const record = purchaseOrder({ status: 'approved', flowdown_frozen: true, quality_clause_codes: null });
    if (snapshot === undefined) delete (record as Record<string, unknown>).flowdown_snapshot;
    else (record as Record<string, unknown>).flowdown_snapshot = snapshot;
    mockedApi.getPurchaseOrder.mockResolvedValue(record as any);
    renderDetail();

    const frozen = await screen.findByTestId('po-frozen-flowdown');
    expect(frozen).toHaveTextContent('Frozen when the PO was issued.');
    expect(frozen).toHaveTextContent(expected);
    expect(screen.queryByRole('checkbox', { name: /^QX/ })).toBeNull();
  });

  test('an API without flowdown_frozen still treats a non-draft PO as frozen', async () => {
    const record = purchaseOrder({ status: 'sent', quality_clause_codes: ['QX1'] });
    delete (record as Record<string, unknown>).flowdown_frozen;
    delete (record as Record<string, unknown>).flowdown_snapshot;
    mockedApi.getPurchaseOrder.mockResolvedValue(record as any);
    renderDetail();

    expect(await screen.findByTestId('po-frozen-flowdown')).toHaveTextContent('Frozen when the PO was issued.');
    expect(screen.queryByRole('checkbox', { name: /^QX/ })).toBeNull();
  });
});
