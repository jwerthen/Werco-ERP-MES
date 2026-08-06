/**
 * StockMovementsPanel — the inventory ledger view.
 *
 * The four properties locked here are the ones a plausible "cleanup" would
 * break, each for a reason that only shows up in production data:
 *
 *  1. **Source reads `reference_number`, never `reference_id`.** On the headline
 *     row shape (`work_order_operation` — per-operation material-tie consumption,
 *     i.e. the laser-nest case this panel was built for) `reference_id` is an
 *     OPERATION id. Rendering it as the job would mislabel exactly the rows that
 *     matter most. The fixture below gives the two fields DIFFERENT values so a
 *     regression cannot pass by coincidence.
 *  2. **Transfer rows are excluded from the totals.** A transfer carries a
 *     POSITIVE quantity for a ZERO net on-hand change; counting it invents stock
 *     that never arrived.
 *  3. **Date bounds are Central days resolved to UTC instants.** A bare
 *     `YYYY-MM-DD` would be compared against UTC-stored timestamps and push
 *     second-shift movements into the wrong day.
 *  4. **Server pagination over-fetches one row.** The endpoint returns no total
 *     count, so `hasNext` comes from the overflow row — which must not be
 *     rendered.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

import api from '../../services/api';
import StockMovementsPanel from './StockMovementsPanel';
import type { InventoryTransaction } from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getInventoryTransactions: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const txn = (over: Partial<InventoryTransaction> & { id: number }): InventoryTransaction => ({
  company_id: 1,
  part_id: 500,
  transaction_type: 'issue',
  quantity: -4,
  created_at: '2026-08-05T14:30:00Z',
  part: { id: 500, part_number: 'SHT-0500', name: '0.125 CRS Sheet', unit_of_measure: 'each' },
  ...over,
});

beforeEach(() => {
  jest.clearAllMocks();
});

describe('StockMovementsPanel', () => {
  it('names the job from reference_number, not reference_id, on operation-scoped consumption', async () => {
    mockedApi.getInventoryTransactions.mockResolvedValue([
      txn({
        id: 1,
        reference_type: 'work_order_operation',
        // Deliberately different from the work order number: reference_id is the
        // OPERATION id on this shape. If the UI ever renders 9987, it is reading
        // the wrong field.
        reference_id: 9987,
        reference_number: 'WO-1042',
        lot_number: 'LOT-77',
        notes: 'Material consumption for work order WO-1042 operation 20',
      }),
    ]);

    render(<StockMovementsPanel />);

    // NOTE: DataTable renders the desktop table AND the mobile cards into the
    // DOM (CSS hides one per breakpoint; jsdom applies no breakpoints), so every
    // cell value legitimately appears more than once. All-variants throughout.
    expect(await screen.findAllByText('WO-1042')).not.toHaveLength(0);
    expect(screen.getAllByText('Operation completed').length).toBeGreaterThan(0);
    expect(screen.queryByText('9987')).not.toBeInTheDocument();
    expect(screen.getAllByText('SHT-0500').length).toBeGreaterThan(0);
    expect(screen.getAllByText('LOT-77').length).toBeGreaterThan(0);
  });

  it('excludes transfer rows from the page totals (they are net-zero on hand)', async () => {
    mockedApi.getInventoryTransactions.mockResolvedValue([
      txn({ id: 1, transaction_type: 'issue', quantity: -4 }),
      txn({ id: 2, transaction_type: 'receive', quantity: 10 }),
      // Positive quantity, but moves between locations — zero net change.
      txn({ id: 3, transaction_type: 'transfer', quantity: 25, from_location: 'A-1', to_location: 'B-2' }),
    ]);

    render(<StockMovementsPanel />);

    await waitFor(() => expect(screen.getAllByText('SHT-0500').length).toBeGreaterThan(0));

    const outTile = screen.getByText('Out (this page)').closest('div')?.parentElement;
    const inTile = screen.getByText('In (this page)').closest('div')?.parentElement;
    const netTile = screen.getByText('Net (this page)').closest('div')?.parentElement;

    // 4 out, 10 in, net +6 — the transfer's 25 appears in NONE of them.
    expect(within(outTile as HTMLElement).getByText('4')).toBeInTheDocument();
    expect(within(inTile as HTMLElement).getByText('10')).toBeInTheDocument();
    expect(within(netTile as HTMLElement).getByText('+6')).toBeInTheDocument();
  });

  it('sends Central day boundaries as UTC instants, not bare dates', async () => {
    mockedApi.getInventoryTransactions.mockResolvedValue([]);
    render(<StockMovementsPanel />);
    await waitFor(() => expect(mockedApi.getInventoryTransactions).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-08-05' } });

    await waitFor(() => expect(mockedApi.getInventoryTransactions).toHaveBeenCalledTimes(2));
    const params = mockedApi.getInventoryTransactions.mock.calls.at(-1)?.[0];

    // 2026-08-05 is CDT (UTC−5), so Central midnight is 05:00Z the same day —
    // NOT '2026-08-05' and NOT 00:00Z.
    expect(params?.start_date).toBe('2026-08-05T05:00:00.000Z');
    expect(params?.start_date).not.toBe('2026-08-05');
  });

  it('over-fetches one row for hasNext and does not render the overflow row', async () => {
    // 51 rows for a page size of 50: the 51st exists only to prove a next page.
    const rows = Array.from({ length: 51 }, (_, i) =>
      txn({ id: i + 1, reference_number: `WO-${1000 + i}`, reference_type: 'work_order_operation' })
    );
    mockedApi.getInventoryTransactions.mockResolvedValue(rows);

    render(<StockMovementsPanel />);

    await screen.findAllByText('WO-1000');
    expect(mockedApi.getInventoryTransactions.mock.calls[0][0]).toMatchObject({ limit: 51, offset: 0 });
    // The 51st row (WO-1050) is the overflow probe — it must not be rendered.
    expect(screen.queryByText('WO-1050')).not.toBeInTheDocument();
    screen.getAllByLabelText('Next page').forEach((btn) => expect(btn).not.toBeDisabled());
  });

  it('changing a filter returns to the first page', async () => {
    mockedApi.getInventoryTransactions.mockResolvedValue(
      Array.from({ length: 51 }, (_, i) => txn({ id: i + 1, reference_number: `WO-${2000 + i}` }))
    );
    render(<StockMovementsPanel />);
    // Wait for the RESPONSE to land, not just the call: Next is disabled until
    // the overflow row has set hasNext, so clicking earlier is a silent no-op.
    await screen.findAllByText('WO-2000');

    fireEvent.click(screen.getAllByLabelText('Next page')[0]);
    await waitFor(() =>
      expect(mockedApi.getInventoryTransactions.mock.calls.at(-1)?.[0]).toMatchObject({ offset: 50 })
    );

    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-08-01' } });
    await waitFor(() =>
      expect(mockedApi.getInventoryTransactions.mock.calls.at(-1)?.[0]).toMatchObject({ offset: 0 })
    );
  });

  it('points an empty unfiltered ledger at the material-tie explanation', async () => {
    mockedApi.getInventoryTransactions.mockResolvedValue([]);
    render(<StockMovementsPanel />);

    expect(await screen.findAllByText('No stock movements recorded yet')).not.toHaveLength(0);
    expect(screen.getAllByText(/may not have material tied to them/i).length).toBeGreaterThan(0);
  });

  it('renders the shared error state with a working retry', async () => {
    mockedApi.getInventoryTransactions.mockRejectedValueOnce(new Error('boom'));
    render(<StockMovementsPanel />);

    const retry = (await screen.findAllByRole('button', { name: /retry/i }))[0];
    mockedApi.getInventoryTransactions.mockResolvedValueOnce([txn({ id: 1, reference_number: 'WO-9' })]);
    fireEvent.click(retry);

    expect(await screen.findAllByText('WO-9')).not.toHaveLength(0);
  });
});
