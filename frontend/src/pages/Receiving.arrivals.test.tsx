/**
 * Receiving — PO arrival classification + optional lot number.
 *
 * Covers:
 *  - classifyPOArrival (pure): today / overdue / later / unscheduled, the
 *    expected_date ?? required_date ?? earliest-open-line-required_date
 *    precedence, closed-line exclusion, and datetime-string normalization.
 *  - The "Arriving Today" summary strip always renders (0 when nothing is due),
 *    counts today's/overdue POs, and the PO cards get Today/Overdue badges with
 *    overdue-first ordering.
 *  - The receive flow no longer blocks on a blank Lot # — the payload carries
 *    lot_number: undefined so the backend auto-assigns the receipt number.
 *
 * The api service + AuthContext are mocked at the module boundary — no real
 * network (same pattern as the sibling Receiving page tests).
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ReceivingPage, { classifyPOArrival } from './Receiving';
import api from '../services/api';
import { getCentralTodayISODate } from '../utils/centralTime';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getOpenPOsForReceiving: jest.fn(),
    getReceivingLocations: jest.fn(),
    getReceivingStats: jest.fn(),
    getInspectionQueue: jest.fn(),
    getReceivingHistory: jest.fn(),
    getPOForReceiving: jest.fn(),
    receiveNewMaterial: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, role: 'manager' }, isAuthenticated: true, isLoading: false }),
}));

const mockApi = api as jest.Mocked<typeof api>;

// ---------------------------------------------------------------------------
// classifyPOArrival — pure unit tests (fixed "today" so they're deterministic)
// ---------------------------------------------------------------------------

const TODAY = '2026-07-17';

describe('classifyPOArrival', () => {
  it('classifies expected_date == today as today', () => {
    expect(classifyPOArrival({ expected_date: '2026-07-17', required_date: null, lines: [] }, TODAY)).toEqual({
      status: 'today',
      date: '2026-07-17',
    });
  });

  it('classifies a past date as overdue', () => {
    expect(classifyPOArrival({ expected_date: '2026-07-10' }, TODAY)).toEqual({
      status: 'overdue',
      date: '2026-07-10',
    });
  });

  it('classifies a future date as later', () => {
    expect(classifyPOArrival({ required_date: '2026-08-01' }, TODAY)).toEqual({
      status: 'later',
      date: '2026-08-01',
    });
  });

  it('classifies a PO with no dates anywhere as unscheduled', () => {
    expect(classifyPOArrival({ expected_date: null, required_date: null, lines: [] }, TODAY)).toEqual({
      status: 'unscheduled',
      date: null,
    });
    // Fields entirely absent (open-pos payload before the backend adds expected_date).
    expect(classifyPOArrival({}, TODAY)).toEqual({ status: 'unscheduled', date: null });
  });

  it('prefers expected_date over required_date', () => {
    expect(
      classifyPOArrival({ expected_date: '2026-07-17', required_date: '2026-07-01', lines: [] }, TODAY),
    ).toEqual({ status: 'today', date: '2026-07-17' });
  });

  it('falls back to the earliest required_date across OPEN lines', () => {
    const po = {
      expected_date: null,
      required_date: null,
      lines: [
        { required_date: '2026-07-20', is_closed: false },
        { required_date: '2026-07-01', is_closed: true }, // closed — ignored
        { required_date: '2026-07-17', is_closed: false }, // earliest open
        { required_date: null, is_closed: false },
      ],
    };
    expect(classifyPOArrival(po, TODAY)).toEqual({ status: 'today', date: '2026-07-17' });
  });

  it('normalizes datetime strings down to their date part', () => {
    expect(classifyPOArrival({ expected_date: '2026-07-17T00:00:00Z' }, TODAY)).toEqual({
      status: 'today',
      date: '2026-07-17',
    });
  });
});

// ---------------------------------------------------------------------------
// Page render tests — strip, badges, ordering, optional lot submit
// ---------------------------------------------------------------------------

/** Shift a YYYY-MM-DD string by whole days (UTC-safe for date-only math). */
const shiftDays = (iso: string, days: number): string => {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
};

const openLine = (id: number, requiredDate: string | null = null) => ({
  line_id: id,
  line_number: 1,
  part_id: 7,
  part_number: 'PN-555',
  part_name: 'Bracket',
  quantity_ordered: 10,
  quantity_received: 5,
  quantity_remaining: 5,
  unit_price: 12.5,
  required_date: requiredDate,
  is_closed: false,
});

const makePO = (
  id: number,
  poNumber: string,
  dates: { expected_date?: string | null; required_date?: string | null },
  lines = [openLine(id * 100)],
) => ({
  po_id: id,
  po_number: poNumber,
  vendor_id: 3,
  vendor_name: 'Acme Metals',
  vendor_code: 'VND-001',
  order_date: null,
  required_date: dates.required_date ?? null,
  expected_date: dates.expected_date ?? null,
  status: 'sent',
  lines,
  total_lines: lines.length,
});

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={['/receiving']}>
      <ReceivingPage />
    </MemoryRouter>,
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getOpenPOsForReceiving.mockResolvedValue([]);
  mockApi.getReceivingLocations.mockResolvedValue([]);
  mockApi.getReceivingStats.mockResolvedValue({
    pending_inspection: 0,
    receipts_in_period: 0,
    acceptance_rate: 100,
    rejections_in_period: 0,
  });
  mockApi.getInspectionQueue.mockResolvedValue([]);
  mockApi.getReceivingHistory.mockResolvedValue([]);
});

describe('Receiving — Arriving Today strip', () => {
  it('always renders, showing a trustworthy 0 when nothing is due today', async () => {
    renderPage();

    const strip = await screen.findByTestId('arriving-today-strip');
    expect(within(strip).getByText('0')).toBeInTheDocument();
    expect(within(strip).getByText(/expected today/i)).toBeInTheDocument();
    expect(within(strip).queryByText(/overdue/i)).toBeNull();
  });

  it('counts today + overdue POs, badges the cards, and sorts overdue first', async () => {
    const today = getCentralTodayISODate();
    mockApi.getOpenPOsForReceiving.mockResolvedValue([
      makePO(1, 'PO-LATER', { required_date: shiftDays(today, 7) }),
      makePO(2, 'PO-TODAY', { expected_date: today }),
      makePO(3, 'PO-OVERDUE', { expected_date: shiftDays(today, -3) }),
    ]);
    renderPage();

    const strip = await screen.findByTestId('arriving-today-strip');
    // 1 expected today (with its open-line count) + 1 overdue.
    expect(within(strip).getByText('1', { selector: '.text-amber-300' })).toBeInTheDocument();
    expect(within(strip).getByText(/1 open line$/i)).toBeInTheDocument();
    expect(within(strip).getByText('1', { selector: '.text-red-400' })).toBeInTheDocument();
    expect(within(strip).getByText(/overdue/i)).toBeInTheDocument();

    // Card badges.
    const todayCard = screen.getByText('PO-TODAY').closest('button') as HTMLElement;
    expect(within(todayCard).getByText('Today')).toBeInTheDocument();
    const overdueCard = screen.getByText('PO-OVERDUE').closest('button') as HTMLElement;
    expect(within(overdueCard).getByText('Overdue')).toBeInTheDocument();
    const laterCard = screen.getByText('PO-LATER').closest('button') as HTMLElement;
    expect(within(laterCard).queryByText('Today')).toBeNull();
    expect(within(laterCard).queryByText('Overdue')).toBeNull();

    // Ordering: overdue → today → later.
    const cardOrder = screen
      .getAllByText(/^PO-/)
      .map((el) => el.textContent)
      .filter((t) => t === 'PO-OVERDUE' || t === 'PO-TODAY' || t === 'PO-LATER');
    expect(cardOrder).toEqual(['PO-OVERDUE', 'PO-TODAY', 'PO-LATER']);
  });

  it('sorts unscheduled POs last, with date-then-po_number tiebreaks within a status', async () => {
    const today = getCentralTodayISODate();
    mockApi.getOpenPOsForReceiving.mockResolvedValue([
      makePO(1, 'PO-UNSCHED', {}, [openLine(100, null)]),
      makePO(2, 'PO-LATER-FAR', { required_date: shiftDays(today, 7) }),
      // Same status + same date as the next one — the po_number breaks the tie.
      makePO(3, 'PO-SAME-DAY-B', { required_date: shiftDays(today, 3) }),
      makePO(4, 'PO-SAME-DAY-A', { required_date: shiftDays(today, 3) }),
      makePO(5, 'PO-TODAY', { expected_date: today }),
    ]);
    renderPage();

    await screen.findByTestId('arriving-today-strip');
    await screen.findByText('PO-TODAY');
    const cardOrder = screen
      .getAllByText(/^PO-/)
      .map((el) => el.textContent)
      .filter((t) => t?.startsWith('PO-'));
    expect(cardOrder).toEqual(['PO-TODAY', 'PO-SAME-DAY-A', 'PO-SAME-DAY-B', 'PO-LATER-FAR', 'PO-UNSCHED']);
  });
});

describe('Receiving — optional lot number', () => {
  it('submits with a blank lot (lot_number undefined) and no validation error', async () => {
    const today = getCentralTodayISODate();
    const po = makePO(1, 'PO-1001', { expected_date: today });
    mockApi.getOpenPOsForReceiving.mockResolvedValue([po]);
    mockApi.getPOForReceiving.mockResolvedValue(po);
    mockApi.receiveNewMaterial.mockResolvedValue({ id: 99, receipt_number: 'RCV-20260717-001' });
    renderPage();

    // Select the PO, then the line's Receive button to open the modal.
    fireEvent.click(await screen.findByText('PO-1001'));
    fireEvent.click(await screen.findByRole('button', { name: 'Receive' }));
    const dialog = await screen.findByRole('dialog');

    // Lot # is optional now: no required marker, helper text explains the default.
    const lotInput = within(dialog).getByLabelText(/^Lot Number$/i);
    expect(lotInput).not.toBeRequired();
    expect(lotInput).not.toHaveAttribute('aria-required');
    expect(
      within(dialog).getByText(/left blank, the receipt number is auto-assigned as the lot/i),
    ).toBeInTheDocument();

    // Submit with the lot left blank ("Receive Material" is also the tab label,
    // so scope to the dialog).
    fireEvent.click(within(dialog).getByRole('button', { name: 'Receive Material' }));

    await waitFor(() =>
      expect(mockApi.receiveNewMaterial).toHaveBeenCalledWith(
        expect.objectContaining({ po_line_id: 100, quantity_received: 5, lot_number: undefined }),
      ),
    );
    expect(screen.queryByText(/lot number is required/i)).toBeNull();
    await waitFor(() => expect(screen.getByText('Material received successfully')).toBeInTheDocument());
  });

  it('still sends a trimmed vendor lot when one is entered', async () => {
    const po = makePO(1, 'PO-1001', { expected_date: null, required_date: null });
    mockApi.getOpenPOsForReceiving.mockResolvedValue([po]);
    mockApi.getPOForReceiving.mockResolvedValue(po);
    mockApi.receiveNewMaterial.mockResolvedValue({ id: 100 });
    renderPage();

    fireEvent.click(await screen.findByText('PO-1001'));
    fireEvent.click(await screen.findByRole('button', { name: 'Receive' }));
    const dialog = await screen.findByRole('dialog');

    fireEvent.change(within(dialog).getByLabelText(/^Lot Number$/i), { target: { value: '  LOT-42  ' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Receive Material' }));

    await waitFor(() =>
      expect(mockApi.receiveNewMaterial).toHaveBeenCalledWith(
        expect.objectContaining({ lot_number: 'LOT-42' }),
      ),
    );
  });
});
