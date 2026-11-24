/**
 * Labor Review — the CSV export walk and the work-center filter.
 *
 * What these pin:
 *  - Export CSV walks EVERY page of the filtered result (not the visible page), sending the
 *    page's own filters at the export page size, and the file carries every row;
 *  - the walk stops at 10,000 rows (50 pages of 200) and says so with a warning toast;
 *  - an export failure shows the server's refusal verbatim and downloads nothing;
 *  - picking a work center puts work_center_id in the request and drops back to page 1.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import { ToastProvider } from '../components/ui';
import LaborReview, { LABOR_REVIEW_PAGE_SIZE } from './LaborReview';
import type { LaborReviewEntry, LaborReviewListParams, LaborReviewListResponse } from '../types/laborReview';
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

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'supervisor', is_superuser: false },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

const mockedApi = api as jest.Mocked<typeof api>;
const EXPORT_PAGE_SIZE = 200;

const makeEntry = (id: number, employee_name: string): LaborReviewEntry => ({
  id,
  version: 1,
  user_id: 7,
  employee_name,
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
  quantity_scrapped: 0,
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
  flag_counts: {},
  long_entry_hours: 12,
  long_zero_pieces_hours: 4,
  ...overrides,
});

const workCenters: WorkCenter[] = [
  { id: 3, version: 1, code: 'BENCH', name: 'Bench', work_center_type: 'assembly' } as WorkCenter,
  { id: 4, version: 1, code: 'LASER', name: 'Laser One', work_center_type: 'laser' } as WorkCenter,
];

const renderPage = (path = '/labor-review') =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <ToastProvider>
        <LaborReview />
      </ToastProvider>
    </MemoryRouter>
  );

const exportCalls = (): LaborReviewListParams[] =>
  mockedApi.listLaborReviewEntries.mock.calls
    .map(call => call[0] ?? {})
    .filter(params => params.page_size === EXPORT_PAGE_SIZE);

const RealBlob = window.Blob;
let blobParts: { parts: BlobPart[]; type?: string }[] = [];
let clickSpy: jest.SpyInstance;
let createUrlSpy: jest.Mock;
let revokeUrlSpy: jest.Mock;

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getWorkCenters.mockResolvedValue(workCenters);
  blobParts = [];
  // Record what the page hands to new Blob(...) so the CSV text can be read back.
  // A plain constructor function (not a subclass: ts-jest's downlevelled super() cannot
  // call jsdom's native Blob class) that returns a real Blob.
  function RecordingBlob(parts?: BlobPart[], options?: BlobPropertyBag): Blob {
    blobParts.push({ parts: parts ?? [], type: options?.type });
    return new RealBlob(parts, options);
  }
  window.Blob = RecordingBlob as unknown as typeof Blob;
  clickSpy = jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  createUrlSpy = jest.fn(() => 'blob:mock');
  revokeUrlSpy = jest.fn();
  Object.defineProperty(URL, 'createObjectURL', { value: createUrlSpy, configurable: true, writable: true });
  Object.defineProperty(URL, 'revokeObjectURL', { value: revokeUrlSpy, configurable: true, writable: true });
});

afterEach(() => {
  window.Blob = RealBlob;
  clickSpy.mockRestore();
});

const csvText = (): string => {
  const csvBlob = blobParts.find(blob => blob.type?.startsWith('text/csv'));
  expect(csvBlob).toBeDefined();
  return csvBlob!.parts.join('');
};

describe('LaborReview CSV export', () => {
  it('walks every page with the active filters and writes every row', async () => {
    mockedApi.listLaborReviewEntries.mockImplementation(async (params: LaborReviewListParams = {}) => {
      if (params.page_size !== EXPORT_PAGE_SIZE) {
        return makeList([makeEntry(1, 'Visible Person')], { total: 3, has_next: true });
      }
      if (params.page === 1) {
        return makeList([makeEntry(1, 'Visible Person'), makeEntry(2, 'Second Person')], {
          total: 3,
          has_next: true,
          page_size: EXPORT_PAGE_SIZE,
        });
      }
      return makeList([makeEntry(3, 'Third Person')], { total: 3, page: 2, page_size: EXPORT_PAGE_SIZE });
    });
    renderPage('/labor-review?status=closed&flag=overlap&page=2');
    const exportButton = await screen.findByRole('button', { name: /Export CSV/ });
    await waitFor(() => expect(exportButton).toBeEnabled());

    fireEvent.click(exportButton);

    await waitFor(() => expect(clickSpy).toHaveBeenCalledTimes(1));
    const calls = exportCalls();
    expect(calls.map(params => params.page)).toEqual([1, 2]);
    for (const params of calls) {
      expect(params).toEqual(expect.objectContaining({ status: 'closed', flag: 'overlap' }));
    }

    const csv = csvText();
    const lines = csv.trim().split('\n');
    expect(lines).toHaveLength(4); // header + 3 rows across both pages
    expect(lines[0]).toContain('Time entry ID');
    expect(lines[0]).toContain('Production day');
    expect(lines[0]).not.toMatch(/Actions/);
    expect(csv).toContain('Visible Person');
    expect(csv).toContain('Second Person');
    expect(csv).toContain('Third Person');
    expect(createUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeUrlSpy).toHaveBeenCalledWith('blob:mock');
  });

  it('stops at 50 pages and warns that the file is the first 10,000 rows', async () => {
    let next = 0;
    mockedApi.listLaborReviewEntries.mockImplementation(async (params: LaborReviewListParams = {}) => {
      if (params.page_size !== EXPORT_PAGE_SIZE) {
        return makeList([makeEntry(1, 'Visible Person')], { total: 20000, has_next: true });
      }
      const items = Array.from({ length: EXPORT_PAGE_SIZE }, () => {
        next += 1;
        return makeEntry(next, `Person ${next}`);
      });
      return makeList(items, { total: 20000, has_next: true, page: params.page, page_size: EXPORT_PAGE_SIZE });
    });
    renderPage();
    const exportButton = await screen.findByRole('button', { name: /Export CSV/ });
    await waitFor(() => expect(exportButton).toBeEnabled());

    fireEvent.click(exportButton);

    expect(
      await screen.findByText('Exported the first 10000 entries. Narrow the filters to export the rest.')
    ).toBeInTheDocument();
    expect(exportCalls()).toHaveLength(50);
    expect(clickSpy).toHaveBeenCalledTimes(1);
  });

  it('shows the server refusal verbatim and downloads nothing when a page fails', async () => {
    mockedApi.listLaborReviewEntries.mockImplementation(async (params: LaborReviewListParams = {}) => {
      if (params.page_size !== EXPORT_PAGE_SIZE) {
        return makeList([makeEntry(1, 'Visible Person')], { total: 1 });
      }
      throw { response: { status: 422, data: { detail: 'A date range may cover at most 62 production days' } } };
    });
    renderPage();
    const exportButton = await screen.findByRole('button', { name: /Export CSV/ });
    await waitFor(() => expect(exportButton).toBeEnabled());

    fireEvent.click(exportButton);

    expect(await screen.findByText('A date range may cover at most 62 production days')).toBeInTheDocument();
    expect(clickSpy).not.toHaveBeenCalled();
    expect(createUrlSpy).not.toHaveBeenCalled();
  });
});

describe('LaborReview work-center filter', () => {
  it('sends the picked work center and returns to page 1', async () => {
    mockedApi.listLaborReviewEntries.mockResolvedValue(makeList([makeEntry(1, 'Visible Person')]));
    renderPage('/labor-review?page=3');
    const select = (await screen.findByLabelText('Work center')) as HTMLSelectElement;
    await waitFor(() => expect(within(select).getByRole('option', { name: 'Laser One' })).toBeInTheDocument());

    fireEvent.change(select, { target: { value: '4' } });

    await waitFor(() => {
      const calls = mockedApi.listLaborReviewEntries.mock.calls;
      expect(calls[calls.length - 1][0]).toEqual(expect.objectContaining({ work_center_id: 4, page: 1 }));
    });
    expect(select.value).toBe('4');
  });
});
