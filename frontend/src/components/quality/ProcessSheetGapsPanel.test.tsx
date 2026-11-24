import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ProcessSheetGapsPanel, { PROCESS_SHEET_GAP_LIMIT } from './ProcessSheetGapsPanel';
import api from '../../services/api';
import type { WorkOrderProcessSheetGapResponse } from '../../types/workOrderProcessSheet';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { getProcessSheetGaps: jest.fn() },
}));

jest.mock('../../hooks/usePermissions', () => ({
  usePermissions: () => ({ can: () => true, role: 'quality', isSuperuser: false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const RESPONSE: WorkOrderProcessSheetGapResponse = {
  total: 1,
  items: [
    {
      work_order_id: 12,
      work_order_number: 'WO-20261120-004',
      status: 'released',
      part_id: 1,
      part_number: 'PN-4410',
      part_name: 'Frame, welded',
      quantity_ordered: 2,
      due_date: '2026-12-04',
      customer_name: 'Northwind Fabrication',
      created_at: '2026-11-20T15:00:00Z',
      operation_count: 3,
      expected_process_sheet_steps: [
        {
          routing_operation_id: 31,
          operation_number: '20',
          operation_sequence: 20,
          name: 'Weld',
          component_part_id: null,
          component_part_number: null,
          process_sheet_id: 7,
          sheet_number: 'PS-104',
        },
      ],
    },
  ],
};

function renderPanel() {
  render(
    <MemoryRouter>
      <ProcessSheetGapsPanel />
    </MemoryRouter>
  );
}

beforeEach(() => jest.clearAllMocks());

it('lists live work orders missing their sheet steps, linked to the work order', async () => {
  mockedApi.getProcessSheetGaps.mockResolvedValue(RESPONSE);
  renderPanel();
  const link = await screen.findAllByRole('link', { name: 'WO-20261120-004' });
  expect(link[0]).toHaveAttribute('href', '/work-orders/12');
  expect(screen.getAllByText('Op 20 Weld (sheet PS-104)').length).toBeGreaterThan(0);
  expect(mockedApi.getProcessSheetGaps).toHaveBeenCalledWith(PROCESS_SHEET_GAP_LIMIT);
});

it('says when the list is truncated', async () => {
  mockedApi.getProcessSheetGaps.mockResolvedValue({ ...RESPONSE, total: 240 });
  renderPanel();
  expect(await screen.findByText(/Showing the first 1 of 240 work orders/)).toBeInTheDocument();
});

it('renders the empty state', async () => {
  mockedApi.getProcessSheetGaps.mockResolvedValue({ total: 0, items: [] });
  renderPanel();
  expect((await screen.findAllByText('No work orders are missing process sheets')).length).toBeGreaterThan(0);
});

it('renders an error with a retry that re-runs the read', async () => {
  mockedApi.getProcessSheetGaps.mockRejectedValueOnce(new Error('Offline')).mockResolvedValueOnce(RESPONSE);
  renderPanel();
  fireEvent.click(await screen.findByRole('button', { name: /retry/i }));
  await waitFor(() => expect(mockedApi.getProcessSheetGaps).toHaveBeenCalledTimes(2));
  expect((await screen.findAllByRole('link', { name: 'WO-20261120-004' })).length).toBeGreaterThan(0);
});
