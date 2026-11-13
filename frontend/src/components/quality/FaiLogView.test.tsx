/**
 * The FAI log of record view: server-paged (one row over-fetched for "has next"),
 * filters and page in the URL, row click-through, and the Admin/Manager-only audited
 * export of the same filtered log.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import FaiLogView, { FAI_LOG_PAGE_SIZE } from './FaiLogView';
import { ToastProvider } from '../ui/Toast';
import api from '../../services/api';
import type { FAILogRow } from '../../types/quality';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getFAILog: jest.fn(),
    exportFAILog: jest.fn(),
  },
}));

const mocked = api as jest.Mocked<typeof api>;

const row = (index: number, overrides: Partial<FAILogRow> = {}): FAILogRow => ({
  fai_id: index,
  fai_number: `FAI-20261113-${String(index).padStart(3, '0')}`,
  log_date: '2026-11-13',
  part_id: 1,
  part_number: 'PN-7731',
  part_name: 'Bracket, hinge',
  part_revision: 'B',
  part_current_revision: 'B',
  customer: 'Acme Aerospace',
  customer_po: 'PO-889',
  work_order_id: 55,
  job_number: 'WO-1055',
  serial_number: null,
  status: 'passed',
  is_complete: true,
  fai_type: 'full',
  reason: 'new_part',
  total_characteristics: 3,
  characteristics_passed: 3,
  characteristics_failed: 0,
  inspection_date: '2026-11-12',
  completed_date: '2026-11-13',
  customer_approval_required: true,
  customer_approved: false,
  customer_approval_date: null,
  created_at: '2026-11-12T15:00:00Z',
  ...overrides,
});

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="query">{location.search}</output>;
}

function renderLog({ canExport = true, entry = '/quality?tab=fai&fai_view=log' } = {}) {
  const onOpenFai = jest.fn();
  render(
    <ToastProvider>
      <MemoryRouter initialEntries={[entry]}>
        <FaiLogView onOpenFai={onOpenFai} canExport={canExport} />
        <LocationProbe />
      </MemoryRouter>
    </ToastProvider>
  );
  return { onOpenFai };
}

const query = () => new URLSearchParams(screen.getByTestId('query').textContent || '');

beforeEach(() => {
  jest.clearAllMocks();
  mocked.getFAILog.mockResolvedValue([
    row(1, { is_complete: true }),
    row(2, { is_complete: false, status: 'pending' }),
  ]);
});

test('loads the first page newest-first and renders the paper log columns', async () => {
  const { onOpenFai } = renderLog();

  await waitFor(() => expect(mocked.getFAILog).toHaveBeenCalledWith({ limit: FAI_LOG_PAGE_SIZE + 1, offset: 0 }));
  const table = await screen.findByRole('table');
  for (const header of ['FAI Report #', 'Date', 'Part #', 'Description', 'Customer', 'Job #', 'Complete']) {
    expect(within(table).getByRole('columnheader', { name: header })).toBeInTheDocument();
  }
  const firstRow = within(table).getByText('FAI-20261113-001').closest('tr')!;
  expect(firstRow).toHaveTextContent('WO-1055');
  expect(firstRow).toHaveTextContent('X');
  expect(firstRow).toHaveTextContent('Required');

  fireEvent.click(firstRow);
  expect(onOpenFai).toHaveBeenCalledWith(1);
});

test('pages through the server with the page kept in the URL', async () => {
  mocked.getFAILog.mockResolvedValueOnce(Array.from({ length: FAI_LOG_PAGE_SIZE + 1 }, (_, i) => row(i + 1)));
  renderLog();

  const next = (await screen.findAllByRole('button', { name: 'Next page' }))[0];
  await waitFor(() => expect(next).toBeEnabled());
  fireEvent.click(next);

  await waitFor(() =>
    expect(mocked.getFAILog).toHaveBeenLastCalledWith({ limit: FAI_LOG_PAGE_SIZE + 1, offset: FAI_LOG_PAGE_SIZE })
  );
  expect(query().get('log_page')).toBe('2');
});

test('filters come from the URL and are sent as inclusive Central dates', async () => {
  renderLog({
    entry: '/quality?tab=fai&fai_view=log&log_status=passed&log_from=2026-01-01&log_to=2026-06-30&log_page=3',
  });

  await waitFor(() =>
    expect(mocked.getFAILog).toHaveBeenCalledWith({
      status: 'passed',
      start_date: '2026-01-01',
      end_date: '2026-06-30',
      limit: FAI_LOG_PAGE_SIZE + 1,
      offset: 2 * FAI_LOG_PAGE_SIZE,
    })
  );
  expect(screen.getByLabelText('Result')).toHaveValue('passed');
});

test('a filter change returns to the first page; typed search is debounced into the URL', async () => {
  jest.useFakeTimers();
  try {
    renderLog({ entry: '/quality?tab=fai&fai_view=log&log_page=4' });
    await act(async () => {});

    fireEvent.change(screen.getByLabelText('Search'), { target: { value: 'WO-1055' } });
    expect(query().get('log_q')).toBeNull();
    await act(async () => {
      jest.advanceTimersByTime(350);
    });

    expect(query().get('log_q')).toBe('WO-1055');
    expect(query().get('log_page')).toBeNull();
    await act(async () => {});
    expect(mocked.getFAILog).toHaveBeenLastCalledWith({ search: 'WO-1055', limit: FAI_LOG_PAGE_SIZE + 1, offset: 0 });
  } finally {
    jest.useRealTimers();
  }
});

test('Export log downloads the same filtered log as Excel', async () => {
  mocked.exportFAILog.mockResolvedValue({ blob: new Blob(['x']), filename: 'fai_log.xlsx' });
  const click = jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  try {
    renderLog({ entry: '/quality?tab=fai&fai_view=log&log_customer=Acme' });
    fireEvent.click(await screen.findByRole('button', { name: /Export log/ }));

    await waitFor(() => expect(mocked.exportFAILog).toHaveBeenCalledWith({ customer: 'Acme', format: 'xlsx' }));
    await waitFor(() => expect(click).toHaveBeenCalled());
  } finally {
    click.mockRestore();
  }
});

test('an export refusal is shown in the server words, read out of the blob body', async () => {
  const detail = 'Export exceeds the 10000-row limit. Narrow the filters and try again.';
  const body = { text: () => Promise.resolve(JSON.stringify({ detail })) };
  mocked.exportFAILog.mockRejectedValue({ response: { status: 400, data: body } });
  renderLog();

  fireEvent.click(await screen.findByRole('button', { name: /Export log/ }));
  expect(await screen.findByText(detail)).toBeInTheDocument();
});

test('roles outside the export gate see the log but no export', async () => {
  renderLog({ canExport: false });
  await screen.findAllByText('FAI-20261113-001');
  expect(screen.queryByRole('button', { name: /Export log/ })).not.toBeInTheDocument();
});

test('a failed load offers a retry', async () => {
  mocked.getFAILog.mockRejectedValueOnce(new Error('offline'));
  renderLog();
  fireEvent.click(await screen.findByRole('button', { name: /retry/i }));
  await waitFor(() => expect(mocked.getFAILog).toHaveBeenCalledTimes(2));
});
