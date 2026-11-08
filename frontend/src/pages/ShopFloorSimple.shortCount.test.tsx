/**
 * Short count on the desktop / phone shop floor (owner ruling 1, 2026-11-05).
 *
 * A short count at operation completion is reported as scrap with a reason, or the
 * operation stays open — never closed short at the counted quantity. Pinned here:
 *  - "remaining" is what is still UNACCOUNTED FOR: received (target less any upstream
 *    short) less good less scrap;
 *  - Check Out carries "Complete Op N after checking out", offered only once the entry
 *    accounts for every received unit (otherwise it says how many are missing);
 *  - ticked, it is two writes in order — the clock-out, then `/complete` with no
 *    quantity — unless the clock-out completed the operation on its own
 *    (`operation_status: "complete"`), in which case there is nothing left to close;
 *  - a close refused after the clock-out landed is reported honestly;
 *  - the work-center queue fallback carries the REAL scrap total and the server's
 *    short-count figures (it used to hard-code `quantity_scrapped: 0`);
 *  - a check-in whose operation is NOT in the loaded list (another station, a filter)
 *    still carries those figures off `my-active-job`, so its Check Out judges against
 *    what can reach the operation, not the full target;
 *  - on a POOLED job whose earlier operation already declared a loss
 *    (`upstream_short_uncredited`), the hint never says to enter it as scrap.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import ShopFloorSimple from './ShopFloorSimple';
import api from '../services/api';
import { WorkCenter } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getShopFloorOperations: jest.fn(),
    getWorkCenterQueue: jest.fn(),
    getWorkCenters: jest.fn(),
    getDashboard: jest.fn(),
    getMyActiveJob: jest.fn(),
    getScrapReasonCodes: jest.fn(),
    clockOut: jest.fn(),
    completeOperation: jest.fn(),
  },
}));

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ can: () => false }),
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, company_id: 1, role: 'operator' } }),
}));

jest.mock('../context/CompanyContext', () => ({
  useCompany: () => ({ currentCompany: { id: 1 } }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const DEBURR_1: WorkCenter = {
  id: 1,
  version: 1,
  code: 'DEB1',
  name: 'Deburr 1',
  work_center_type: 'assembly',
  hourly_rate: 60,
  capacity_hours_per_day: 8,
  efficiency_factor: 1,
  is_active: true,
  current_status: 'available',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

const OP = {
  id: 101,
  work_order_id: 42,
  work_order_number: 'WO-2026-0042',
  part_number: 'PN-0099',
  part_name: 'Mount Plate',
  operation_number: '20',
  operation_name: 'Deburr',
  description: null,
  work_center_id: 1,
  work_center_name: 'Deburr 1',
  status: 'in_progress',
  quantity_ordered: 10,
  quantity_complete: 6,
  quantity_scrapped: 0,
  priority: 3,
  due_date: null,
  customer_name: null,
  customer_po: null,
  actual_start: null,
  setup_instructions: null,
  run_instructions: null,
  requires_inspection: false,
};

const ACTIVE_JOB = {
  time_entry_id: 501,
  clock_in: new Date(Date.now() - 60_000).toISOString(),
  entry_type: 'run',
  work_order_id: 42,
  operation_id: 101,
  work_center_id: 1,
  work_order_number: 'WO-2026-0042',
  part_number: 'PN-0099',
  part_name: 'Mount Plate',
  operation_name: 'Deburr',
  operation_number: '20',
  quantity_ordered: 10,
  quantity_complete: 6,
};

function renderShopFloor(path = '/shop-floor/operations') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/shop-floor/operations" element={<ShopFloorSimple />} />
      </Routes>
    </MemoryRouter>
  );
}

async function openCheckOut() {
  const card = await screen.findByTestId('shop-floor-op-101');
  fireEvent.click(within(card).getByRole('button', { name: 'Check Out' }));
  return screen.findByRole('dialog');
}

function setNumber(dialog: HTMLElement, label: string, value: number) {
  fireEvent.change(within(dialog).getByLabelText(label), { target: { value: String(value) } });
}

/** The legacy scrap-reason picker (no company codes): a Headless UI listbox. */
function pickScrapReason(label: string) {
  fireEvent.click(screen.getByRole('combobox', { name: 'Scrap reason' }));
  fireEvent.mouseDown(screen.getByRole('option', { name: new RegExp(label, 'i') }));
}

describe('ShopFloorSimple — short count (owner ruling 1)', () => {
  beforeAll(() => {
    window.HTMLElement.prototype.scrollIntoView = jest.fn();
  });

  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    mockedApi.getWorkCenters.mockResolvedValue([]);
    mockedApi.getDashboard.mockResolvedValue({ work_centers: [] });
    mockedApi.getScrapReasonCodes.mockResolvedValue([]);
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [OP] });
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [] });
  });

  it('offers "complete after" only once the check-out accounts for every received unit', async () => {
    renderShopFloor();
    const dialog = await openCheckOut();
    const box = within(dialog).getByLabelText('Complete Op 20 after checking out');

    // 6 of 10 recorded and nothing entered: 4 still unaccounted for.
    expect(box).toBeDisabled();
    expect(
      within(dialog).getByText(/4 not yet accounted for — enter lost pieces as scrap with a reason/)
    ).toBeInTheDocument();

    // 3 good + 1 scrap covers it.
    setNumber(dialog, 'Additional good parts at checkout', 3);
    setNumber(dialog, 'Scrap', 1);
    expect(box).toBeEnabled();
    expect(within(dialog).queryByText(/not yet accounted for — enter lost pieces/)).not.toBeInTheDocument();

    // Back to short: the box disables again (and would not be honored if it were ticked).
    setNumber(dialog, 'Scrap', 0);
    expect(box).toBeDisabled();
  });

  it('ticked: clocks out the good, then closes with the covering scrap and NO quantity — two writes, in order', async () => {
    mockedApi.clockOut.mockResolvedValue({ operation_status: 'in_progress' });
    mockedApi.completeOperation.mockResolvedValue({});
    renderShopFloor();
    const dialog = await openCheckOut();

    setNumber(dialog, 'Additional good parts at checkout', 3);
    setNumber(dialog, 'Scrap', 1);
    pickScrapReason('Material defect');
    fireEvent.click(within(dialog).getByLabelText('Complete Op 20 after checking out'));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Check out & complete' }));

    // The scrap that covers the short rides the CLOSE, which bounds it by what is still
    // unaccounted under the row lock; the clock-out carries the good only.
    await waitFor(() =>
      expect(mockedApi.completeOperation).toHaveBeenCalledWith(
        101,
        expect.objectContaining({ source: 'desktop', quantity_scrapped: 1, scrap_reason: 'Material defect' })
      )
    );
    expect(mockedApi.clockOut).toHaveBeenCalledWith(
      501,
      expect.objectContaining({ quantity_produced: 3, quantity_scrapped: 0 })
    );
    expect(mockedApi.clockOut.mock.calls[0][1]).not.toHaveProperty('scrap_reason');
    expect(mockedApi.clockOut.mock.invocationCallOrder[0]).toBeLessThan(
      mockedApi.completeOperation.mock.invocationCallOrder[0]
    );
    expect(await screen.findByText('Completed WO-2026-0042')).toBeInTheDocument();
  });

  it('a full-count clock-out that already completed the op sends no /complete, ticked or not', async () => {
    mockedApi.clockOut.mockResolvedValue({ operation_status: 'complete' });
    renderShopFloor();
    const dialog = await openCheckOut();

    setNumber(dialog, 'Additional good parts at checkout', 4);
    fireEvent.click(within(dialog).getByLabelText('Complete Op 20 after checking out'));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Check out & complete' }));

    expect(await screen.findByText('Completed WO-2026-0042')).toBeInTheDocument();
    expect(mockedApi.completeOperation).not.toHaveBeenCalled();
  });

  it('a full good count is said up front: the box is checked and disabled, because the check-out completes it', async () => {
    renderShopFloor();
    const dialog = await openCheckOut();
    setNumber(dialog, 'Additional good parts at checkout', 4);
    const box = within(dialog).getByLabelText('Complete Op 20 after checking out');
    expect(box).toBeChecked();
    expect(box).toBeDisabled();
    expect(within(dialog).getByText(/That is the full count — checking out completes/)).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Check out & complete' })).toBeInTheDocument();
  });

  it('unticked: a plain check-out that leaves the operation open', async () => {
    mockedApi.clockOut.mockResolvedValue({ operation_status: 'in_progress' });
    renderShopFloor();
    const dialog = await openCheckOut();

    setNumber(dialog, 'Additional good parts at checkout', 2);
    fireEvent.click(within(dialog).getByRole('button', { name: 'End time and save' }));

    expect(await screen.findByText('Checked out of WO-2026-0042')).toBeInTheDocument();
    expect(mockedApi.completeOperation).not.toHaveBeenCalled();
  });

  it('says so honestly when the check-out landed but the close was refused', async () => {
    mockedApi.clockOut.mockResolvedValue({ operation_status: 'in_progress' });
    mockedApi.completeOperation.mockRejectedValue({
      response: {
        status: 409,
        data: {
          detail:
            'Bob T is still clocked in to Op 20 — have them clock out with their count first, or leave the operation open.',
        },
      },
    });
    renderShopFloor();
    const dialog = await openCheckOut();

    setNumber(dialog, 'Additional good parts at checkout', 3);
    setNumber(dialog, 'Scrap', 1);
    pickScrapReason('Material defect');
    fireEvent.click(within(dialog).getByLabelText('Complete Op 20 after checking out'));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Check out & complete' }));

    // The 1 scrap covering the short rode the refused close (the check-out carried 0), so
    // it was NOT saved — said plainly, as the crew station does.
    const failed = await screen.findByText(
      'Checked out, but completing failed (the 1 scrap was not saved): Bob T is still clocked in to Op 20 — have them clock out with their count first, or leave the operation open.'
    );
    // An ERROR, not a warning: the check-out landed, the close was refused.
    expect(failed.closest('[role="alert"]')).toHaveClass('bg-red-600');
  });

  it('a card accounted for by scrap keeps +1 and Report quantity beside the close banner (replacements)', async () => {
    // 8 good + 2 scrapped of 10: the operation CAN close, but good may still rise to the
    // server's ceiling of 10 — the operator re-making the 2 must not be cut off.
    mockedApi.getShopFloorOperations.mockResolvedValue({
      operations: [{ ...OP, quantity_complete: 8, quantity_scrapped: 2, quantity_good_ceiling: 10 }],
    });
    renderShopFloor();
    const card = await screen.findByTestId('shop-floor-op-101');
    expect(within(card).getByText('All pieces accounted for')).toBeInTheDocument();
    expect(within(card).getByRole('button', { name: /\+1 Complete/ })).toBeEnabled();
    expect(within(card).getByRole('button', { name: 'Report quantity' })).toBeInTheDocument();
  });

  it('a card at its full good count offers only the close (no more good fits)', async () => {
    mockedApi.getShopFloorOperations.mockResolvedValue({
      operations: [{ ...OP, quantity_complete: 10, quantity_scrapped: 0, quantity_good_ceiling: 10 }],
    });
    renderShopFloor();
    const card = await screen.findByTestId('shop-floor-op-101');
    expect(within(card).getByText('Target quantity reached')).toBeInTheDocument();
    expect(within(card).queryByRole('button', { name: /\+1 Complete/ })).not.toBeInTheDocument();
  });

  it('works from the received count and subtracts recorded scrap in "not yet accounted for"', async () => {
    // Target 10, 2 closed short upstream, 5 good + 1 scrapped here → 2 left.
    mockedApi.getShopFloorOperations.mockResolvedValue({
      operations: [{ ...OP, quantity_complete: 5, quantity_scrapped: 1, upstream_quantity_short: 2 }],
    });
    renderShopFloor();
    const dialog = await openCheckOut();

    expect(within(dialog).getByText(/Not yet accounted for:\s*2/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Remaining' }));
    expect(within(dialog).getByLabelText('Additional good parts at checkout')).toHaveValue(2);
    expect(within(dialog).getByLabelText('Complete Op 20 after checking out')).toBeEnabled();
  });

  it('the work-center queue fallback carries the REAL scrap total and the short-count figures', async () => {
    // No list rows for the station → the page falls back to the kiosk queue read.
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [] });
    mockedApi.getWorkCenters.mockResolvedValue([DEBURR_1]);
    mockedApi.getWorkCenterQueue.mockResolvedValue({
      queue: [
        {
          operation_id: 101,
          work_order_id: 42,
          work_order_number: 'WO-2026-0042',
          part_number: 'PN-0099',
          part_name: 'Mount Plate',
          operation_number: '20',
          operation_name: 'Deburr',
          work_center_id: 1,
          status: 'in_progress',
          quantity_ordered: 10,
          quantity_complete: 7,
          quantity_scrapped: 3,
          upstream_quantity_short: 0,
          quantity_good_ceiling: 10,
          quantity_unaccounted: 0,
          priority: 3,
          due_date: null,
        },
      ],
    });
    localStorage.setItem('shopfloor:workCenterId', '1');
    renderShopFloor('/shop-floor/operations?work_center_id=1');

    // 7 good + 3 scrap = 10: accounted for, so the card offers the close. With the old
    // hard-coded `quantity_scrapped: 0` this read "3 remaining" and offered nothing.
    expect(await screen.findByText('All pieces accounted for')).toBeInTheDocument();
  });
  describe('a check-in whose operation is not in the loaded list', () => {
    // Sequenced job, 10 target: Op 10 closed 2 short, so Op 20 can receive 8; 6 good here.
    const SHORTED_JOB = {
      ...ACTIVE_JOB,
      upstream_quantity_short: 2,
      quantity_good_ceiling: 8,
      quantity_unaccounted: 2,
    };

    async function openBannerCheckOut() {
      const banner = await screen.findByRole('button', { name: /^Go to WO-2026-0042/ });
      fireEvent.click(within(banner.closest('li') as HTMLElement).getByRole('button', { name: 'Check Out' }));
      return screen.findByRole('dialog');
    }

    it('judges against what can reach the operation, off my-active-job', async () => {
      // The desk list is filtered elsewhere: the checked-in operation is not loaded.
      mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [] });
      mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [SHORTED_JOB], active_job: SHORTED_JOB });
      renderShopFloor();
      const dialog = await openBannerCheckOut();

      // 8 received − 6 good = 2 (the full target would have said 4).
      expect(within(dialog).getByText(/Not yet accounted for:\s*2/)).toBeInTheDocument();
      fireEvent.click(within(dialog).getByRole('button', { name: 'Remaining' }));
      expect(within(dialog).getByLabelText('Additional good parts at checkout')).toHaveValue(2);
      // 6 + 2 is the full count the server auto-completes — said up front, and no hint to
      // scrap the 2 an upstream close already declared short.
      expect(within(dialog).getByText(/That is the full count — checking out completes/)).toBeInTheDocument();
      expect(within(dialog).queryByText(/enter lost pieces as scrap/)).not.toBeInTheDocument();
    });

    it("prefers the check-in's figures over a stale list row", async () => {
      // The list row predates the upstream short close; my-active-job is the fresher read.
      mockedApi.getShopFloorOperations.mockResolvedValue({
        operations: [{ ...OP, upstream_quantity_short: 0, quantity_good_ceiling: 10, quantity_unaccounted: 4 }],
      });
      mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [SHORTED_JOB], active_job: SHORTED_JOB });
      renderShopFloor();
      const dialog = await openBannerCheckOut();

      expect(within(dialog).getByText(/Not yet accounted for:\s*2/)).toBeInTheDocument();
    });
  });

  describe('a POOLED job whose earlier operation already declared the loss', () => {
    it('the check-out hint says not to scrap pieces never received — never "enter lost pieces as scrap"', async () => {
      // Pooled: no chain credit, so 4 still read as unaccounted, 2 of them already closed
      // short at Op 10 (`upstream_short_uncredited`).
      mockedApi.getShopFloorOperations.mockResolvedValue({
        operations: [{ ...OP, upstream_quantity_short: 0, quantity_good_ceiling: 10, upstream_short_uncredited: 2 }],
      });
      renderShopFloor();
      const dialog = await openCheckOut();

      const hint = within(dialog).getByText(/^4 not yet accounted for — /);
      expect(hint).toHaveTextContent(
        "2 pcs were already closed short at an earlier operation, but this job's operations are not sequenced"
      );
      expect(hint).toHaveTextContent('Do not report pieces you never received as scrap');
      expect(hint).toHaveTextContent('Check out and leave it open.');
      expect(hint).not.toHaveTextContent(/enter lost pieces as scrap/);
    });

    it('the work-center queue fallback carries the uncredited figure too', async () => {
      mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [] });
      mockedApi.getWorkCenters.mockResolvedValue([DEBURR_1]);
      mockedApi.getWorkCenterQueue.mockResolvedValue({
        queue: [
          {
            operation_id: 101,
            work_order_id: 42,
            work_order_number: 'WO-2026-0042',
            part_number: 'PN-0099',
            part_name: 'Mount Plate',
            operation_number: '20',
            operation_name: 'Deburr',
            work_center_id: 1,
            status: 'in_progress',
            quantity_ordered: 10,
            quantity_complete: 6,
            quantity_scrapped: 0,
            upstream_quantity_short: 0,
            quantity_good_ceiling: 10,
            quantity_unaccounted: 4,
            upstream_short_uncredited: 2,
            priority: 3,
            due_date: null,
          },
        ],
      });
      localStorage.setItem('shopfloor:workCenterId', '1');
      renderShopFloor('/shop-floor/operations?work_center_id=1');
      const dialog = await openCheckOut();

      expect(within(dialog).getByText(/^4 not yet accounted for — /)).toHaveTextContent(
        'Do not report pieces you never received as scrap'
      );
    });
  });
});
