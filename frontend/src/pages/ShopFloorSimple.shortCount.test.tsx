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
 *    short-count figures (it used to hard-code `quantity_scrapped: 0`).
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

  it('ticked: clocks out, then closes with NO quantity — two writes, in that order', async () => {
    mockedApi.clockOut.mockResolvedValue({ operation_status: 'in_progress' });
    mockedApi.completeOperation.mockResolvedValue({});
    renderShopFloor();
    const dialog = await openCheckOut();

    setNumber(dialog, 'Additional good parts at checkout', 3);
    setNumber(dialog, 'Scrap', 1);
    pickScrapReason('Material defect');
    fireEvent.click(within(dialog).getByLabelText('Complete Op 20 after checking out'));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Check out & complete' }));

    await waitFor(() => expect(mockedApi.completeOperation).toHaveBeenCalledWith(101, { source: 'desktop' }));
    expect(mockedApi.clockOut).toHaveBeenCalledWith(
      501,
      expect.objectContaining({ quantity_produced: 3, quantity_scrapped: 1, scrap_reason: 'Material defect' })
    );
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

    const failed = await screen.findByText(
      'Checked out, but completing failed: Bob T is still clocked in to Op 20 — have them clock out with their count first, or leave the operation open.'
    );
    // An ERROR, not a warning: the check-out landed, the close was refused.
    expect(failed.closest('[role="alert"]')).toHaveClass('bg-red-600');
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
});
