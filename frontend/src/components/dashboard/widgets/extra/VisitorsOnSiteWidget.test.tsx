import React from 'react';
import { fireEvent, screen } from '@testing-library/react';
import { readySource, renderWithDashboardContext } from '../../../../test-utils/dashboardWidgets';
import type { VisitorLogListResponse, VisitorLogResponse } from '../../../../types/visitor';
import { VisitorsOnSiteWidget } from './VisitorsOnSiteWidget';

function visitor(overrides: Partial<VisitorLogResponse> = {}): VisitorLogResponse {
  return {
    id: 1,
    visitor_name: 'Dana Ortiz',
    visitor_company: 'Boeing Quality',
    visitor_phone: '555-0142',
    host_name: 'Jon W.',
    host_user_id: 4,
    purpose: 'audit',
    purpose_note: null,
    safety_acknowledged: true,
    status: 'signed_in',
    signed_in_at: new Date().toISOString(),
    signed_out_at: null,
    signin_station_id: 1,
    station_label: 'Lobby',
    entered_by_user_id: null,
    ...overrides,
  };
}

function list(items: VisitorLogResponse[], total = items.length): VisitorLogListResponse {
  return { items, total };
}

describe('VisitorsOnSiteWidget', () => {
  it('lists visitors with company and host, never their phone number', () => {
    renderWithDashboardContext(<VisitorsOnSiteWidget />, {
      sources: {
        visitors: readySource(
          list([visitor(), visitor({ id: 2, visitor_name: 'Sam Lee', visitor_company: null, host_name: null })], 12)
        ),
      },
    });
    expect(screen.getByText('12 on site')).toBeInTheDocument();
    expect(screen.getByText('12 on site total')).toBeInTheDocument();
    expect(screen.getByText('Dana Ortiz')).toBeInTheDocument();
    expect(screen.getByText('· Boeing Quality')).toBeInTheDocument();
    expect(screen.getByText('Host: Jon W.')).toBeInTheDocument();
    expect(screen.getByText('No host named')).toBeInTheDocument();
    expect(screen.queryByText(/555-0142/)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open visitor log' })).toHaveAttribute('href', '/visitor-log');
  });

  it('shows the date as well as the time for a visitor signed in before today', () => {
    renderWithDashboardContext(<VisitorsOnSiteWidget />, {
      sources: { visitors: readySource(list([visitor({ signed_in_at: '2026-11-02T20:15:00Z' })])) },
    });
    // 20:15 UTC on Nov 2 is 2:15 PM Central (CST).
    expect(screen.getByText('Nov 2, 2:15 PM')).toBeInTheDocument();
  });

  it('renders the empty state', () => {
    renderWithDashboardContext(<VisitorsOnSiteWidget />, { sources: { visitors: readySource(list([])) } });
    expect(screen.getByText('No visitors on site')).toBeInTheDocument();
    expect(screen.getByText('0 on site')).toBeInTheDocument();
  });

  it('keeps stale data when a refresh fails', () => {
    renderWithDashboardContext(<VisitorsOnSiteWidget />, {
      sources: { visitors: readySource(list([visitor()]), { error: 'Could not refresh' }) },
    });
    expect(screen.getByText('Last verified · refresh failed')).toBeInTheDocument();
    expect(screen.getByText('Dana Ortiz')).toBeInTheDocument();
  });

  it('forces a refresh from the error state', () => {
    const { refresh } = renderWithDashboardContext(<VisitorsOnSiteWidget />, {
      sources: { visitors: readySource(null, { error: 'Could not refresh' }) },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(refresh).toHaveBeenCalledWith(true);
  });

  it('is not available on a 403', () => {
    renderWithDashboardContext(<VisitorsOnSiteWidget />, {
      sources: { visitors: readySource(null, { forbidden: true }) },
    });
    expect(screen.getByText('Not available for your role')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('omits the visitor-log link when it cannot be opened', () => {
    renderWithDashboardContext(<VisitorsOnSiteWidget />, {
      sources: { visitors: readySource(list([visitor()])) },
      env: { canOpen: () => false },
    });
    expect(screen.getByText('Dana Ortiz')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });
});
