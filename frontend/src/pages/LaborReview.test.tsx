/**
 * Labor Review — the page over GET /shop-floor/time-entries.
 *
 * What these pin:
 *  - URL params ARE the query: a deep link's filters and page reach the request, and a
 *    malformed value is dropped rather than sent;
 *  - row actions come from the server's can_* fields, never re-derived client-side;
 *  - approve / unapprove / set-clock-out are NON-optimistic: nothing on screen changes
 *    until the server answers, a refusal is shown verbatim, and the list is re-read;
 *  - the close-clock dialog sends the entry's version, a trimmed reason and a UTC instant.
 *
 * DataTable renders the desktop table AND mobile cards into jsdom, so row assertions are
 * scoped to the desktop `data-table`.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import { ToastProvider } from '../components/ui';
import LaborReview, { LABOR_REVIEW_PAGE_SIZE, paramsFromSearch } from './LaborReview';
import type { LaborReviewEntry, LaborReviewListResponse, SetClockOutResponse } from '../types/laborReview';
import type { WorkCenter } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    listLaborReviewEntries: jest.fn(),
    getWorkCenters: jest.fn(),
    approveTimeEntry: jest.fn(),
    unapproveTimeEntry: jest.fn(),
    setTimeEntryClockOut: jest.fn(),
  },
}));

const mockUser: { value: { id: number; role: string; is_superuser: boolean } } = {
  value: { id: 1, role: 'supervisor', is_superuser: false },
};
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockUser.value, isAuthenticated: true, isLoading: false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const makeEntry = (overrides: Partial<LaborReviewEntry> = {}): LaborReviewEntry => ({
  id: 501,
  version: 3,
  user_id: 7,
  employee_name: 'Dana Fictional',
  employee_id: 'E-007',
  work_order_id: 42,
  work_order_number: 'WO-1042',
  work_order_status: 'in_progress',
  operation_id: 900,
  operation_number: '10',
  operation_name: 'Deburr',
  operation_status: 'in_progress',
  work_center_id: 3,
  work_center_name: 'Bench',
  entry_type: 'run',
  source: 'kiosk',
  clock_in: '2026-11-20T13:00:00Z',
  clock_out: '2026-11-20T21:00:00Z',
  duration_hours: 8,
  elapsed_hours: 8,
  is_open: false,
  quantity_produced: 12,
  quantity_scrapped: 1,
  approved: null,
  approved_by: null,
  approved_by_name: null,
  production_day: '2026-11-20',
  shift_name: 'First',
  shift_end: '2026-11-20T21:30:00Z',
  flags: [],
  can_approve: true,
  can_unapprove: false,
  can_set_clock_out: true,
  ...overrides,
});

const makeList = (
  items: LaborReviewEntry[],
  overrides: Partial<LaborReviewListResponse> = {}
): LaborReviewListResponse => ({
  items,
  total: items.length,
  page: 1,
  page_size: LABOR_REVIEW_PAGE_SIZE,
  has_next: false,
  start_date: '2026-11-17',
  end_date: '2026-11-23',
  flag_counts: { long_entry: 2, overlap: 1 },
  long_entry_hours: 12,
  long_zero_pieces_hours: 4,
  ...overrides,
});

const workCenter: WorkCenter = {
  id: 3,
  version: 1,
  code: 'BENCH',
  name: 'Bench',
  work_center_type: 'assembly',
} as WorkCenter;

const renderPage = (path = '/labor-review') =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <ToastProvider>
        <LaborReview />
      </ToastProvider>
    </MemoryRouter>
  );

const table = () => screen.getByTestId('data-table');
const lastListParams = () => {
  const calls = mockedApi.listLaborReviewEntries.mock.calls;
  return calls[calls.length - 1][0] ?? {};
};

beforeEach(() => {
  jest.clearAllMocks();
  mockUser.value = { id: 1, role: 'supervisor', is_superuser: false };
  mockedApi.getWorkCenters.mockResolvedValue([workCenter]);
});

describe('paramsFromSearch', () => {
  it('reads every filter and drops malformed values', () => {
    const params = paramsFromSearch(
      new URLSearchParams(
        'start_date=2026-11-01&end_date=bad&status=open&approved=false&flag=overlap&flagged_only=true&user_id=7&work_center_id=x&work_order_id=42&page=3'
      )
    );
    expect(params).toEqual({
      page_size: LABOR_REVIEW_PAGE_SIZE,
      start_date: '2026-11-01',
      status: 'open',
      approved: false,
      flag: 'overlap',
      flagged_only: true,
      user_id: 7,
      work_order_id: 42,
      page: 3,
    });
  });

  it('ignores an unknown flag and status', () => {
    const params = paramsFromSearch(new URLSearchParams('flag=made_up&status=sideways'));
    expect(params.flag).toBeUndefined();
    expect(params.status).toBeUndefined();
    expect(params.page).toBe(1);
  });
});

describe('LaborReview page', () => {
  it('sends the deep link filters and renders rows with flag chips and a work-order link', async () => {
    mockedApi.listLaborReviewEntries.mockResolvedValue(makeList([makeEntry({ flags: ['long_entry', 'overlap'] })]));
    renderPage('/labor-review?status=closed&work_order_id=42&page=2');

    await waitFor(() => expect(within(table()).getByText('Dana Fictional')).toBeInTheDocument());
    expect(lastListParams()).toEqual(
      expect.objectContaining({ status: 'closed', work_order_id: 42, page: 2, page_size: LABOR_REVIEW_PAGE_SIZE })
    );
    const row = within(table()).getByText('Dana Fictional').closest('tr') as HTMLElement;
    expect(within(row).getByRole('link', { name: 'WO-1042' })).toHaveAttribute('href', '/work-orders/42');
    expect(within(row).getByText('Op 10 · Deburr')).toBeInTheDocument();
    expect(within(row).getByText('Long entry')).toBeInTheDocument();
    expect(within(row).getByText('Overlap')).toBeInTheDocument();
    // Flag summary counts come from flag_counts (every page), not the visible rows.
    expect(screen.getByRole('button', { name: /Long entry\s*2/ })).toBeInTheDocument();
  });

  it('offers only the actions the server allows', async () => {
    mockedApi.listLaborReviewEntries.mockResolvedValue(
      makeList([
        makeEntry({ id: 1, employee_name: 'Approvable Person', can_approve: true, can_set_clock_out: false }),
        makeEntry({
          id: 2,
          employee_name: 'Approved Person',
          approved: '2026-11-21T14:00:00Z',
          approved_by_name: 'Sam Boss',
          can_approve: false,
          can_unapprove: true,
          can_set_clock_out: false,
        }),
        makeEntry({ id: 3, user_id: 1, employee_name: 'Viewer Self', can_approve: false, can_set_clock_out: false }),
      ])
    );
    renderPage();
    await waitFor(() => expect(within(table()).getByText('Approvable Person')).toBeInTheDocument());

    const rowOf = (name: string) => within(table()).getByText(name).closest('tr') as HTMLElement;
    expect(within(rowOf('Approvable Person')).getByRole('button', { name: /^Approve/ })).toBeInTheDocument();
    expect(within(rowOf('Approvable Person')).queryByRole('button', { name: /Close clock|Correct/ })).toBeNull();
    expect(within(rowOf('Approved Person')).getByRole('button', { name: /Clear approval/ })).toBeInTheDocument();
    expect(within(rowOf('Approved Person')).queryByRole('button', { name: /^Approve/ })).toBeNull();
    expect(
      within(rowOf('Viewer Self')).queryByRole('button', { name: /Approve|Clear approval|Close clock|Correct/ })
    ).toBeNull();
    expect(within(rowOf('Viewer Self')).getByText('Your entry')).toBeInTheDocument();
  });

  it('approves non-optimistically and re-reads the list', async () => {
    mockedApi.listLaborReviewEntries
      .mockResolvedValueOnce(makeList([makeEntry()]))
      .mockResolvedValueOnce(
        makeList([
          makeEntry({
            approved: '2026-11-23T15:00:00Z',
            approved_by_name: 'Me',
            can_approve: false,
            can_unapprove: true,
          }),
        ])
      );
    let resolveApprove: () => void = () => undefined;
    mockedApi.approveTimeEntry.mockImplementation(
      () =>
        new Promise(resolve => {
          resolveApprove = () =>
            resolve({
              id: 501,
              user_id: 7,
              clock_in: '',
              clock_out: '',
              duration_hours: 8,
              approved: 'x',
              approved_by: 1,
            });
        })
    );
    renderPage();
    await waitFor(() => expect(within(table()).getByText('Dana Fictional')).toBeInTheDocument());

    fireEvent.click(within(table()).getByRole('button', { name: /^Approve/ }));
    expect(mockedApi.approveTimeEntry).toHaveBeenCalledWith(501);
    // Still unapproved on screen while the server has not answered.
    expect(within(table()).getByText('unapproved')).toBeInTheDocument();

    resolveApprove();
    await waitFor(() => expect(within(table()).getByText('approved')).toBeInTheDocument());
    expect(mockedApi.listLaborReviewEntries).toHaveBeenCalledTimes(2);
  });

  it('shows the server refusal verbatim when approval is refused', async () => {
    mockedApi.listLaborReviewEntries.mockResolvedValue(makeList([makeEntry()]));
    mockedApi.approveTimeEntry.mockRejectedValue({
      response: {
        status: 409,
        data: { detail: 'This time entry is still clocked in. Close the clock before approving it.' },
      },
    });
    renderPage();
    await waitFor(() => expect(within(table()).getByText('Dana Fictional')).toBeInTheDocument());

    fireEvent.click(within(table()).getByRole('button', { name: /^Approve/ }));
    expect(
      await screen.findByText('This time entry is still clocked in. Close the clock before approving it.')
    ).toBeInTheDocument();
    // The row is re-read after a refusal so a stale row is replaced.
    await waitFor(() => expect(mockedApi.listLaborReviewEntries).toHaveBeenCalledTimes(2));
  });

  it('closes a forgotten clock through the dialog with the version, reason and a UTC time', async () => {
    const open = makeEntry({
      clock_out: null,
      duration_hours: null,
      elapsed_hours: 30,
      is_open: true,
      can_approve: false,
      flags: ['open_past_shift_end', 'long_entry'],
    });
    mockedApi.listLaborReviewEntries.mockResolvedValue(makeList([open]));
    const response: SetClockOutResponse = {
      entry: { ...open, clock_out: '2026-11-20T21:30:00Z', is_open: false, version: 4 },
      changed: true,
      previous_clock_out: null,
      previous_duration_hours: null,
      hours: {
        applied: true,
        skipped_reason: null,
        hours_delta: 8.5,
        operation_bucket: 'run',
        operation_hours_before: 1,
        operation_hours_after: 9.5,
        work_order_hours_before: 1,
        work_order_hours_after: 9.5,
        job_cost_recomputed: false,
        actual_cost_recomputed: false,
      },
    };
    mockedApi.setTimeEntryClockOut.mockResolvedValue(response);
    renderPage();
    await waitFor(() => expect(within(table()).getByText('Dana Fictional')).toBeInTheDocument());

    fireEvent.click(within(table()).getByRole('button', { name: 'Close clock for Dana Fictional' }));
    const dialog = await screen.findByRole('dialog');
    // Default time = the end of the shift the entry started in (15:30 Central on 2026-11-20).
    const timeInput = within(dialog).getByLabelText(/Clock-out \(Central time\)/) as HTMLInputElement;
    expect(timeInput.value).toBe('2026-11-20T15:30');

    const submit = within(dialog).getByRole('button', { name: 'Close clock' });
    expect(submit).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: '  Forgot to clock out  ' } });
    fireEvent.click(submit);

    await waitFor(() =>
      expect(mockedApi.setTimeEntryClockOut).toHaveBeenCalledWith(501, {
        clock_out: '2026-11-20T21:30:00.000Z',
        reason: 'Forgot to clock out',
        version: 3,
      })
    );
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(
      await screen.findByText(/Clock-out set for Dana Fictional\. Stored hours adjusted by \+8\.50 h\./)
    ).toBeInTheDocument();
    expect(mockedApi.listLaborReviewEntries).toHaveBeenCalledTimes(2);
  });

  it('keeps the dialog open and shows a stale-version refusal verbatim', async () => {
    mockedApi.listLaborReviewEntries.mockResolvedValue(makeList([makeEntry()]));
    mockedApi.setTimeEntryClockOut.mockRejectedValue({
      response: { status: 409, data: { detail: 'This time entry was modified concurrently. Refresh and retry.' } },
    });
    renderPage();
    await waitFor(() => expect(within(table()).getByText('Dana Fictional')).toBeInTheDocument());

    fireEvent.click(within(table()).getByRole('button', { name: 'Correct clock-out for Dana Fictional' }));
    const dialog = await screen.findByRole('dialog');
    expect((within(dialog).getByLabelText(/Clock-out \(Central time\)/) as HTMLInputElement).value).toBe(
      '2026-11-20T15:00'
    );
    fireEvent.change(within(dialog).getByLabelText(/Clock-out \(Central time\)/), {
      target: { value: '2026-11-20T14:00' },
    });
    fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: 'Left early' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save clock-out' }));

    expect(
      await within(dialog).findByText('This time entry was modified concurrently. Refresh and retry.')
    ).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('refuses a clock-out before the clock-in without calling the server', async () => {
    mockedApi.listLaborReviewEntries.mockResolvedValue(makeList([makeEntry()]));
    renderPage();
    await waitFor(() => expect(within(table()).getByText('Dana Fictional')).toBeInTheDocument());
    fireEvent.click(within(table()).getByRole('button', { name: 'Correct clock-out for Dana Fictional' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/Clock-out \(Central time\)/), {
      target: { value: '2026-11-20T06:00' },
    });
    fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: 'Typo' } });
    expect(within(dialog).getByText("The clock-out must be after the entry's clock-in.")).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Save clock-out' })).toBeDisabled();
    expect(mockedApi.setTimeEntryClockOut).not.toHaveBeenCalled();
  });

  it('filters to one person from the row and back again', async () => {
    mockedApi.listLaborReviewEntries.mockResolvedValue(makeList([makeEntry()]));
    renderPage();
    await waitFor(() => expect(within(table()).getByText('Dana Fictional')).toBeInTheDocument());

    fireEvent.click(within(table()).getByRole('button', { name: "Show only Dana Fictional's entries" }));
    await waitFor(() => expect(lastListParams()).toEqual(expect.objectContaining({ user_id: 7, page: 1 })));
    fireEvent.click(await screen.findByRole('button', { name: /Remove the employee filter/ }));
    await waitFor(() => expect(lastListParams().user_id).toBeUndefined());
  });

  it('renders the shared error state with a working retry', async () => {
    mockedApi.listLaborReviewEntries
      .mockRejectedValueOnce({ response: { status: 500, data: { detail: 'boom' } } })
      .mockResolvedValueOnce(makeList([makeEntry()]));
    renderPage();
    const retry = await screen.findByRole('button', { name: /retry/i });
    fireEvent.click(retry);
    await waitFor(() => expect(within(table()).getByText('Dana Fictional')).toBeInTheDocument());
  });
});
