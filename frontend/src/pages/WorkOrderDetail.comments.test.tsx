/**
 * WorkOrderDetail mounts the Comments panel for the work order it shows.
 *
 * The panel's own behavior (thread, posting, edit/delete, watch, polling) is
 * pinned in components/comments/CommentsPanel.test.tsx; this file pins only the
 * page wiring: the panel is on the page, bound to THIS work order's id, and a
 * comment on it renders inside the page.
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import api from '../services/api';
import { ToastProvider } from '../components/ui';
import { WorkOrder } from '../types';
import type { CommentResponse } from '../types/comments';
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

const comment: CommentResponse = {
  id: 501,
  entity_type: 'work_order',
  entity_id: 77,
  author: { id: 3, display_name: 'Avery Lind' },
  body: 'Customer approved the deviation.',
  created_at: '2026-11-16T15:30:00Z',
  edited_at: null,
  mentions: [],
  can_edit: false,
  can_delete: false,
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
  mockedApi.listComments.mockResolvedValue([comment]);
  mockedApi.getWatchState.mockResolvedValue({ watching: true, watcher_count: 2 });
});

it('mounts the Comments panel for this work order', async () => {
  render(
    <ToastProvider>
      <MemoryRouter initialEntries={['/work-orders/77']}>
        <Routes>
          <Route path="/work-orders/:id" element={<WorkOrderDetail />} />
        </Routes>
      </MemoryRouter>
    </ToastProvider>
  );

  expect(await screen.findByText('Customer approved the deviation.')).toBeInTheDocument();
  expect(mockedApi.listComments).toHaveBeenCalledWith('work_order', 77);
  expect(mockedApi.getWatchState).toHaveBeenCalledWith('work_order', 77);

  const heading = screen.getByRole('heading', { name: 'Comments' });
  const panel = heading.closest('.card') as HTMLElement;
  expect(within(panel).getByRole('combobox', { name: /add a comment/i })).toBeInTheDocument();
  expect(within(panel).getByRole('button', { name: 'Watch' })).toHaveAttribute('aria-pressed', 'true');
  // Any signed-in company user may post — the composer is not role-gated.
  expect(within(panel).getByRole('button', { name: 'Post comment' })).toBeInTheDocument();
});
