import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import JobTimelinePanel from './JobTimelinePanel';
import { JobTimelineResponse } from '../../types/jobPlanning';

jest.mock('../../services/api', () => ({ __esModule: true, default: { getWorkOrderTimeline: jest.fn() } }));
const getTimeline = api.getWorkOrderTimeline as jest.MockedFunction<typeof api.getWorkOrderTimeline>;
const page = (title: string, next: string | null = null): JobTimelineResponse => ({
  items: [
    {
      id: title,
      title,
      occurred_at: '2026-09-07T15:00:00Z',
      category: 'material',
      evidence: 'business_record',
      actor_id: 5,
      actor_name: 'Dana',
      detail: '-4 · lot TEST',
      source_label: 'Stock movement',
      source_url: '/warehouse?tab=inventory&inventory_tab=movements&work_order_id=7',
    },
  ],
  next_cursor: next,
  coverage: 'Business records and scoped audit history.',
});
const setup = () =>
  render(
    <MemoryRouter>
      <JobTimelinePanel workOrderId={7} />
    </MemoryRouter>
  );
beforeEach(() => jest.clearAllMocks());

test('loads on request, pages with cursor, and follows source links', async () => {
  getTimeline.mockResolvedValueOnce(page('Material issued', 'next')).mockResolvedValueOnce(page('Earlier clock-in'));
  setup();
  expect(getTimeline).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Job timeline' }));
  expect(await screen.findByText('Material issued')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Stock movement' })).toHaveAttribute(
    'href',
    expect.stringContaining('work_order_id=7')
  );
  fireEvent.click(screen.getByRole('button', { name: 'Older events' }));
  expect(await screen.findByText('Earlier clock-in')).toBeInTheDocument();
  expect(getTimeline).toHaveBeenLastCalledWith(7, expect.objectContaining({ cursor: 'next' }));
});

test('filters by category, Central date and actor without inventing audit evidence', async () => {
  getTimeline.mockResolvedValue(page('Fixture event'));
  setup();
  fireEvent.click(screen.getByRole('button', { name: 'Job timeline' }));
  await screen.findByText('Fixture event');
  fireEvent.change(screen.getByLabelText('Events'), { target: { value: 'material' } });
  fireEvent.change(screen.getByLabelText('Since (Central)'), { target: { value: '2026-09-07' } });
  await waitFor(() =>
    expect(getTimeline).toHaveBeenLastCalledWith(
      7,
      expect.objectContaining({ category: 'material', start_at: '2026-09-07T05:00:00.000Z' })
    )
  );
  fireEvent.click(await screen.findByRole('button', { name: 'Dana' }));
  await waitFor(() =>
    expect(getTimeline).toHaveBeenLastCalledWith(7, expect.objectContaining({ actor_id: 5, cursor: undefined }))
  );
  expect(screen.getByRole('button', { name: 'Clear actor: Dana' })).toBeInTheDocument();
});

test('failed refresh retains labelled stale evidence and offers retry', async () => {
  getTimeline
    .mockResolvedValueOnce(page('Known history'))
    .mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValueOnce(page('Fresh history'));
  setup();
  fireEvent.click(screen.getByRole('button', { name: 'Job timeline' }));
  await screen.findByText('Known history');
  fireEvent.click(screen.getByRole('button', { name: 'Refresh timeline' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Visible events are stale');
  expect(screen.getByText('Known history')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Retry timeline' }));
  expect(await screen.findByText('Fresh history')).toBeInTheDocument();
});
