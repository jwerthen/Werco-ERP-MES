import React from 'react';
import { fireEvent, screen } from '@testing-library/react';
import { readySource, renderWithDashboardContext } from '../../../../test-utils/dashboardWidgets';
import type { InspectionQueueRow, ReceivingStatsResponse } from '../../../../types/dashboardWidgetData';
import { InspectionQueueWidget } from './InspectionQueueWidget';

const STATS: ReceivingStatsResponse = {
  pending_inspection: 7,
  receipts_in_period: 40,
  inspected_in_period: 33,
  rejections_in_period: 2,
  total_qty_received: 1200,
  total_qty_accepted: 1180,
  total_qty_rejected: 20,
  acceptance_rate: 98.3,
};

function row(id: number, overrides: Partial<InspectionQueueRow> = {}): InspectionQueueRow {
  return {
    receipt_id: id,
    receipt_number: `RCV-${id}`,
    po_number: 'PO-500',
    po_id: 500,
    vendor_name: 'Steel Supply Co',
    part_id: 9,
    part_number: `BAR-${id}`,
    part_name: 'Bar stock',
    quantity_received: '25.0000',
    lot_number: `LOT-${id}`,
    cert_number: null,
    coc_attached: true,
    received_at: '2026-11-05T15:00:00Z',
    received_by_name: 'Dock',
    location_code: null,
    days_pending: 6 - id,
    ...overrides,
  };
}

const QUEUE = [1, 2, 3, 4, 5, 6].map(id => row(id));

describe('InspectionQueueWidget', () => {
  it('shows 30-day stats and the five oldest receipts', () => {
    renderWithDashboardContext(<InspectionQueueWidget />, {
      sources: { inspectionStats: readySource(STATS), inspectionQueue: readySource(QUEUE) },
    });
    expect(screen.getByText('7 pending')).toBeInTheDocument();
    expect(screen.getByText('Rejections').nextSibling).toHaveTextContent('2');
    expect(screen.getByText('Accepted').nextSibling).toHaveTextContent('98.3%');
    expect(screen.getByText('RCV-1')).toBeInTheDocument();
    expect(screen.getByText('RCV-5')).toBeInTheDocument();
    expect(screen.queryByText('RCV-6')).not.toBeInTheDocument();
    expect(screen.getByText('BAR-1 · Steel Supply Co')).toBeInTheDocument();
    expect(screen.getByText('5d')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open receiving and inspection' })).toHaveAttribute(
      'href',
      '/warehouse?tab=receiving'
    );
  });

  it('renders the empty state when nothing is pending', () => {
    renderWithDashboardContext(<InspectionQueueWidget />, {
      sources: { inspectionStats: readySource({ ...STATS, pending_inspection: 0 }), inspectionQueue: readySource([]) },
    });
    expect(screen.getByText('Nothing waiting for inspection')).toBeInTheDocument();
  });

  it('keeps stale data when either refresh fails', () => {
    renderWithDashboardContext(<InspectionQueueWidget />, {
      sources: {
        inspectionStats: readySource(STATS),
        inspectionQueue: readySource(QUEUE, { error: 'Could not refresh' }),
      },
    });
    expect(screen.getByText('Last verified · refresh failed')).toBeInTheDocument();
    expect(screen.getByText('RCV-1')).toBeInTheDocument();
  });

  it('stays loading while one source is still on its way', () => {
    renderWithDashboardContext(<InspectionQueueWidget />, {
      sources: { inspectionStats: readySource(STATS) },
    });
    expect(screen.getByRole('status')).toHaveTextContent('Loading inspection queue…');
  });

  it('forces a refresh when a source failed with nothing to show', () => {
    const { refresh } = renderWithDashboardContext(<InspectionQueueWidget />, {
      sources: {
        inspectionStats: readySource(STATS),
        inspectionQueue: readySource(null, { error: 'Could not refresh' }),
      },
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Inspection queue is unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(refresh).toHaveBeenCalledWith(true);
  });

  it('is not available when either source is forbidden', () => {
    renderWithDashboardContext(<InspectionQueueWidget />, {
      sources: { inspectionStats: readySource(null, { forbidden: true }), inspectionQueue: readySource(QUEUE) },
    });
    expect(screen.getByText('Not available for your role')).toBeInTheDocument();
    expect(screen.queryByText('RCV-1')).not.toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('omits the receiving link when it cannot be opened', () => {
    renderWithDashboardContext(<InspectionQueueWidget />, {
      sources: { inspectionStats: readySource(STATS), inspectionQueue: readySource(QUEUE) },
      env: { canOpen: () => false },
    });
    expect(screen.getByText('RCV-1')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });
});
