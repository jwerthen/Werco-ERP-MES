/**
 * ShipmentTrackingPanel — inline carrier tracking display.
 *
 * Confirms it fetches getTracking and renders the status + events (the
 * replacement for the old prompt()-based manual tracking capture).
 */

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import ShipmentTrackingPanel from './ShipmentTrackingPanel';
import { ToastProvider } from '../ui/Toast';
import api from '../../services/api';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { getTracking: jest.fn() },
}));

const mockApi = api as jest.Mocked<typeof api>;

const renderPanel = (shipmentId = 42) =>
  render(
    <ToastProvider>
      <ShipmentTrackingPanel shipmentId={shipmentId} />
    </ToastProvider>,
  );

beforeEach(() => jest.clearAllMocks());

describe('ShipmentTrackingPanel', () => {
  it('renders tracking status, number, and events', async () => {
    mockApi.getTracking.mockResolvedValueOnce({
      shipment_id: 42,
      shipment_number: 'SHP-1',
      tracking_number: '1Z999',
      tracking_status: 'in_transit',
      events: [
        { id: 1, status: 'in_transit', message: 'Departed facility', location: 'Memphis, TN', occurred_at: '2026-06-08T12:00:00Z' },
      ],
    });

    renderPanel();

    await waitFor(() => expect(screen.getByText('1Z999')).toBeInTheDocument());
    expect(mockApi.getTracking).toHaveBeenCalledWith(42);
    // status is humanized (underscores -> spaces); appears for both badge + event.
    expect(screen.getAllByText('in transit').length).toBeGreaterThan(0);
    expect(screen.getByText(/Departed facility/)).toBeInTheDocument();
  });

  it('shows an empty-events message when there is no history', async () => {
    mockApi.getTracking.mockResolvedValueOnce({
      shipment_id: 42,
      shipment_number: 'SHP-1',
      tracking_status: 'pre_transit',
      events: [],
    });

    renderPanel();

    await waitFor(() => expect(screen.getByText(/no tracking events yet/i)).toBeInTheDocument());
  });
});
