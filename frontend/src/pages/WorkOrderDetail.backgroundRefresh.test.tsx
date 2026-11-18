/**
 * WorkOrderDetail — a failed RELOAD keeps the work order on screen.
 *
 * The page re-reads its work order on a 30 s poll, on window focus, on tab
 * visibility, on realtime pushes and after its own actions. Before this, any of
 * those failing swapped the whole page for the full-page ErrorState, which
 * unmounted the Comments panel and silently threw away an unsent comment draft
 * or an open edit.
 *
 * Pinned here:
 *  - a failed reload of a work order already on screen keeps the page (and the
 *    Comments panel, with its draft) mounted and shows a non-blocking stale
 *    notice with a Retry, which clears it once a reload succeeds;
 *  - a reload that answers 404 (the job was deleted) or 403 still leaves the
 *    page for the full-page ErrorState, as before;
 *  - a failed FIRST load (nothing to show yet) is unchanged: full-page
 *    ErrorState whose Retry loads the page.
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import api from '../services/api';
import { ToastProvider } from '../components/ui';
import { WorkOrder } from '../types';
import WorkOrderDetail from './WorkOrderDetail';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrder: jest.fn(),
    getOperationDetails: jest.fn(),
    getMaterialRequirements: jest.fn(),
    getWorkOrderBlockers: jest.fn(),
    getActiveUsers: jest.fn(),
    getUsers: jest.fn(),
    getDocuments: jest.fn(),
    listComments: jest.fn(),
    getWatchState: jest.fn(),
    getMaterialAllocations: jest.fn(),
    getWorkCenters: jest.fn(),
    getAIRecommendations: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'operator', is_superuser: false },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: jest.fn() }));
jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

const workOrder: WorkOrder = {
  id: 77,
  version: 1,
  work_order_number: 'WO-0077',
  part_id: 100,
  work_order_type: 'production',
  quantity_ordered: 4,
  quantity_complete: 0,
  quantity_scrapped: 0,
  status: 'released',
  priority: 3,
  estimated_hours: 2,
  actual_hours: 0,
  created_at: '2026-11-16T12:00:00Z',
  updated_at: '2026-11-16T12:00:00Z',
  operations: [],
};

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getWorkOrder.mockResolvedValue(workOrder);
  mockedApi.getOperationDetails.mockResolvedValue({ all_operations: [] });
  mockedApi.getMaterialRequirements.mockResolvedValue(null);
  mockedApi.getWorkOrderBlockers.mockResolvedValue([]);
  mockedApi.getActiveUsers.mockResolvedValue([]);
  mockedApi.getUsers.mockResolvedValue([]);
  mockedApi.getDocuments.mockResolvedValue([]);
  mockedApi.getMaterialAllocations.mockResolvedValue([]);
  mockedApi.getWorkCenters.mockResolvedValue([]);
  mockedApi.getAIRecommendations.mockResolvedValue([]);
  mockedApi.listComments.mockResolvedValue([]);
  mockedApi.getWatchState.mockResolvedValue({ watching: false, watcher_count: 0 });
});

function renderDetail() {
  return render(
    <ToastProvider>
      <MemoryRouter initialEntries={['/work-orders/77']}>
        <Routes>
          <Route path="/work-orders/:id" element={<WorkOrderDetail />} />
        </Routes>
      </MemoryRouter>
    </ToastProvider>
  );
}

const composer = () => screen.getByRole('combobox', { name: /add a comment/i }) as HTMLTextAreaElement;
const staleNotice = () => screen.queryByText(/Couldn't refresh this work order/);

/** A background reload — the page re-reads its work order on window focus. */
async function triggerBackgroundReload() {
  const before = mockedApi.getWorkOrder.mock.calls.length;
  await act(async () => {
    window.dispatchEvent(new Event('focus'));
  });
  await waitFor(() => expect(mockedApi.getWorkOrder.mock.calls.length).toBeGreaterThan(before));
}

async function loadedWithDraft(draft: string) {
  renderDetail();
  expect(await screen.findByRole('heading', { name: 'WO-0077' })).toBeInTheDocument();
  await screen.findByText('No comments yet');
  fireEvent.change(composer(), { target: { value: draft } });
}

it('a failed background reload keeps the page and the comment draft, with a stale notice and Retry', async () => {
  await loadedWithDraft('Half-written note about the brake');

  mockedApi.getWorkOrder.mockRejectedValueOnce(new Error('Network Error'));
  await triggerBackgroundReload();

  expect(await screen.findByText(/Couldn't refresh this work order/)).toBeInTheDocument();
  const notice = staleNotice()!.closest('[role="status"]') as HTMLElement;
  expect(notice).toHaveTextContent(/The details below may be out of date\. Last loaded /);
  // The page did not swap to the full-page error: header, panel and draft are all still there.
  expect(screen.queryByText("Couldn't load work order")).not.toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'WO-0077' })).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Comments' })).toBeInTheDocument();
  expect(composer().value).toBe('Half-written note about the brake');

  // Retry re-reads; once a reload succeeds the notice goes and the draft is still intact.
  fireEvent.click(within(notice).getByRole('button', { name: 'Retry' }));
  await waitFor(() => expect(staleNotice()).not.toBeInTheDocument());
  expect(composer().value).toBe('Half-written note about the brake');
});

it('a later successful background reload clears the stale notice on its own', async () => {
  await loadedWithDraft('Draft');
  mockedApi.getWorkOrder.mockRejectedValueOnce({ response: { status: 500, data: { detail: 'Server error' } } });
  await triggerBackgroundReload();
  expect(await screen.findByText(/Couldn't refresh this work order/)).toBeInTheDocument();

  await triggerBackgroundReload();
  await waitFor(() => expect(staleNotice()).not.toBeInTheDocument());
  expect(composer().value).toBe('Draft');
});

it.each([404, 403])('a background reload answering %i still leaves the page for the full-page error', async status => {
  await loadedWithDraft('Draft');
  mockedApi.getWorkOrder.mockRejectedValueOnce({ response: { status, data: { detail: 'Work order not found' } } });
  await triggerBackgroundReload();

  expect(await screen.findByText("Couldn't load work order")).toBeInTheDocument();
  expect(screen.getByText('Failed to load work order')).toBeInTheDocument();
  expect(staleNotice()).not.toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'WO-0077' })).not.toBeInTheDocument();
});

it('a failed first load still shows the full-page ErrorState, and its Retry loads the page', async () => {
  mockedApi.getWorkOrder.mockRejectedValueOnce(new Error('Network Error'));
  renderDetail();

  expect(await screen.findByText("Couldn't load work order")).toBeInTheDocument();
  expect(screen.getByText('Failed to load work order')).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Comments' })).not.toBeInTheDocument();
  expect(staleNotice()).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
  expect(await screen.findByRole('heading', { name: 'WO-0077' })).toBeInTheDocument();
  expect(screen.queryByText("Couldn't load work order")).not.toBeInTheDocument();
});
