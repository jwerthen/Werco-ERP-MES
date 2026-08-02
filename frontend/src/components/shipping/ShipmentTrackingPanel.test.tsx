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

  // The panel colors through the CENTRAL statusColors map (utils/statusColors),
  // not a local style table — this also fixed the delivered-fill drift
  // (bg-emerald-500/20 locally vs the canonical bg-green-500/20).
  describe('status badge uses the central statusColors classes', () => {
    const cases: Array<[string, string[]]> = [
      ['delivered', ['bg-green-500/20', 'text-emerald-300']],
      ['in_transit', ['bg-blue-500/20', 'text-blue-300']],
      ['available_for_pickup', ['bg-amber-500/20', 'text-amber-300']],
      ['return_to_sender', ['bg-red-500/20', 'text-red-300']],
      ['failure', ['bg-red-500/20', 'text-red-300']],
    ];

    it.each(cases)('%s renders the canonical classes', async (status, classes) => {
      mockApi.getTracking.mockResolvedValueOnce({
        shipment_id: 42,
        shipment_number: 'SHP-1',
        tracking_status: status,
        events: [],
      });

      renderPanel();

      const badge = await screen.findByText(status.replace(/_/g, ' '));
      expect(badge).toHaveClass(...classes);
    });

    it('falls back to neutral slate for an unknown status', async () => {
      mockApi.getTracking.mockResolvedValueOnce({
        shipment_id: 42,
        shipment_number: 'SHP-1',
        tracking_status: 'some_new_carrier_state',
        events: [],
      });

      renderPanel();

      const badge = await screen.findByText('some new carrier state');
      expect(badge).toHaveClass('bg-slate-800/50', 'text-slate-400');
    });
  });
});
