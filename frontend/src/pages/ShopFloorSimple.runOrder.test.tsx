/**
 * ShopFloorSimple (/shop-floor/operations "Operations") — server run-order rendering.
 *
 * Owner decision: the manager-dictated Dispatch Board run order is the order
 * operators see EVERYWHERE work is listed. GET /shop-floor/operations now
 * returns rows in the canonical dispatch order (run_order NULLS-LAST, then
 * priority/due date/sequence, grouped by work-center code) and carries the
 * kiosk-identical gap-free `run_order` position. This page must render that
 * payload VERBATIM — the old client dispatch-score re-sort is gone — and show
 * the shared RUN chip on ranked cards.
 *
 * The payload is constructed so the OLD score sort would have inverted it: the
 * LAST operation is an overdue priority-1 job (top dispatch score), while the
 * ranked cards lead with priority 5 and far-future due dates.
 */

import React from 'react';
import { render, screen, within, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import ShopFloorSimple from './ShopFloorSimple';
import api from '../services/api';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getShopFloorOperations: jest.fn(),
    getWorkCenterQueue: jest.fn(),
    getWorkCenters: jest.fn(),
    getDashboard: jest.fn(),
    getMyActiveJob: jest.fn(),
    resolveScanAction: jest.fn(),
    scannerLookup: jest.fn(),
    getOperationDetails: jest.fn(),
  },
}));

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ can: () => false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

function operation(overrides: Record<string, unknown>) {
  return {
    part_number: 'PN-0099',
    part_name: 'Mount Plate',
    operation_number: 'OP10',
    operation_name: 'Laser Cut',
    description: null,
    work_center_id: 1,
    work_center_name: 'Laser 1',
    status: 'ready',
    quantity_ordered: 25,
    quantity_complete: 0,
    quantity_scrapped: 0,
    customer_name: null,
    customer_po: null,
    actual_start: null,
    setup_instructions: null,
    run_instructions: null,
    requires_inspection: false,
    ...overrides,
  };
}

/**
 * SERVER order (canonical dispatch sort): ranked cards first, unranked tail
 * after. The LAST row (WO-8003) is overdue at priority 1 — the old
 * dispatch-score sort would have put it FIRST.
 */
const serverOrderedOperations = [
  operation({ id: 201, work_order_id: 41, work_order_number: 'WO-8001', run_order: 1, priority: 5, due_date: '2099-01-05' }),
  operation({ id: 202, work_order_id: 42, work_order_number: 'WO-8002', run_order: 2, priority: 5, due_date: '2099-01-06' }),
  operation({ id: 203, work_order_id: 43, work_order_number: 'WO-8003', run_order: null, priority: 1, due_date: '2020-01-01' }),
];

function renderShopFloor() {
  return render(
    <MemoryRouter initialEntries={['/shop-floor/operations']}>
      <Routes>
        <Route path="/shop-floor/operations" element={<ShopFloorSimple />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('ShopFloorSimple renders the server run order verbatim', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    mockedApi.getWorkCenters.mockResolvedValue([{ id: 1, name: 'Laser 1', code: 'LASER1' }]);
    mockedApi.getDashboard.mockResolvedValue({ work_centers: [] });
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [], active_job: null });
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: serverOrderedOperations });
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [] });
  });

  it('renders the operation cards in payload order — no client dispatch-score re-sort', async () => {
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-201');

    const cardIds = screen
      .getAllByTestId(/^shop-floor-op-\d+$/)
      .map((card) => card.getAttribute('data-testid'));
    // Verbatim server order. The old score sort would have led with op 203
    // (overdue, P1) — it must stay last.
    expect(cardIds).toEqual(['shop-floor-op-201', 'shop-floor-op-202', 'shop-floor-op-203']);
  });

  it('carries run_order onto the cards: shared RUN chip on ranked cards, none when unranked', async () => {
    renderShopFloor();
    const ranked1 = await screen.findByTestId('shop-floor-op-201');
    const ranked2 = screen.getByTestId('shop-floor-op-202');
    const unranked = screen.getByTestId('shop-floor-op-203');

    expect(within(ranked1).getByTestId('kiosk-run-order-chip')).toHaveAttribute('aria-label', 'Run order 1');
    expect(within(ranked2).getByTestId('kiosk-run-order-chip')).toHaveAttribute('aria-label', 'Run order 2');
    expect(within(unranked).queryByTestId('kiosk-run-order-chip')).not.toBeInTheDocument();
  });

  it('the mobile Next Job strip shows the payload head (the rank-1 job), not the score pick', async () => {
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-201');

    const label = screen.getByText('Next Recommended Job');
    const strip = label.closest('div') as HTMLElement;
    // The top pick is the first row of the SERVER order — the manager's RUN 1 —
    // not the overdue P1 job the old score sort recommended.
    expect(within(strip).getByText('WO-8001')).toBeInTheDocument();
    expect(within(strip).getByTestId('kiosk-run-order-chip')).toHaveAttribute('aria-label', 'Run order 1');
    expect(within(strip).queryByText('WO-8003')).not.toBeInTheDocument();
  });

  it('the work-center-queue fallback mapping carries run_order through to the cards', async () => {
    // Fallback path: a saved work center, no filters, and an empty /operations
    // payload sends the page to getWorkCenterQueue — whose rows must keep their
    // rank so the chip still renders.
    localStorage.setItem('shop_floor_work_center_id', '1');
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [] });
    mockedApi.getWorkCenterQueue.mockResolvedValue({
      queue: [
        {
          operation_id: 301,
          work_order_id: 51,
          work_order_number: 'WO-8888',
          part_number: 'PN-0555',
          part_name: 'Side Panel',
          operation_number: 'OP20',
          operation_name: 'Brake Form',
          status: 'ready',
          quantity_ordered: 10,
          quantity_complete: 0,
          priority: 2,
          due_date: null,
          run_order: 4,
          can_check_in: true,
          blocked_by_previous_operations: false,
          laser_nest: null,
        },
      ],
    });

    renderShopFloor();
    const card = await screen.findByTestId('shop-floor-op-301');
    await waitFor(() => expect(mockedApi.getWorkCenterQueue).toHaveBeenCalledWith(1));

    expect(within(card).getByText('WO-8888')).toBeInTheDocument();
    expect(within(card).getByTestId('kiosk-run-order-chip')).toHaveAttribute('aria-label', 'Run order 4');
  });
});
