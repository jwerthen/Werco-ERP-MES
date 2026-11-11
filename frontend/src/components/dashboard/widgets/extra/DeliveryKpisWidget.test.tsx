import React from 'react';
import { fireEvent, screen } from '@testing-library/react';
import { readySource, renderWithDashboardContext } from '../../../../test-utils/dashboardWidgets';
import type { KpiDashboardResponse, KpiValue } from '../../../../types/dashboardWidgetData';
import { DeliveryKpisWidget } from './DeliveryKpisWidget';

function kpi(value: number | null, overrides: Partial<KpiValue> = {}): KpiValue {
  return { value, target: null, prior_value: null, change_pct: null, trend: 'flat', sparkline: [], ...overrides };
}

function dashboard(overrides: Partial<KpiDashboardResponse> = {}): KpiDashboardResponse {
  return {
    oee: kpi(71),
    on_time_delivery: kpi(90),
    first_pass_yield: kpi(97.25, { target: 98, prior_value: 95, change_pct: 2.4 }),
    scrap_rate: kpi(1.5, { target: 2, prior_value: 2.5, change_pct: -40 }),
    open_ncrs: kpi(4),
    quote_win_rate: kpi(30),
    backlog_hours: kpi(412.4),
    inventory_turnover: kpi(5.1),
    on_time_delivery_ship: kpi(92.04, { target: 95 }),
    otif: kpi(null),
    period_start: '2026-10-12',
    period_end: '2026-11-11',
    generated_at: '2026-11-11T14:05:00Z',
    ...overrides,
  };
}

describe('DeliveryKpisWidget', () => {
  it('shows the five delivery and yield KPIs with targets, trends and an as-of time in Central', () => {
    renderWithDashboardContext(<DeliveryKpisWidget />, { sources: { deliveryKpis: readySource(dashboard()) } });

    expect(screen.getByText('Last 30 days · as of Nov 11, 8:05 AM')).toBeInTheDocument();
    expect(screen.getByText('On-time delivery (shipped)').parentElement).toHaveTextContent('tgt 95.0%');
    expect(screen.getByText('92.0%')).toBeInTheDocument();
    // An uncomputable KPI reads n/a, never 0.
    expect(screen.getByText('OTIF').parentElement).toHaveTextContent('n/a');
    expect(screen.getByText('97.3%')).toBeInTheDocument();
    expect(screen.getByText('1.5%')).toBeInTheDocument();
    expect(screen.getByText('412h')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'First-pass yield up from the prior period' })).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Scrap rate down from the prior period' })).toBeInTheDocument();
    expect(screen.queryByText(/OEE/)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open analytics' })).toHaveAttribute('href', '/analytics');
  });

  it('tolerates an older payload without the ship-based legs', () => {
    renderWithDashboardContext(<DeliveryKpisWidget />, {
      sources: { deliveryKpis: readySource(dashboard({ on_time_delivery_ship: undefined, otif: null })) },
    });
    expect(screen.getByText('On-time delivery (shipped)').parentElement).toHaveTextContent('n/a');
  });

  it('renders the empty state when no KPI can be computed', () => {
    renderWithDashboardContext(<DeliveryKpisWidget />, {
      sources: {
        deliveryKpis: readySource(
          dashboard({
            on_time_delivery_ship: kpi(null),
            otif: kpi(null),
            first_pass_yield: kpi(null),
            scrap_rate: kpi(null),
            backlog_hours: kpi(null),
          })
        ),
      },
    });
    expect(screen.getByText('Not enough activity yet')).toBeInTheDocument();
  });

  it('keeps stale data when a refresh fails', () => {
    renderWithDashboardContext(<DeliveryKpisWidget />, {
      sources: { deliveryKpis: readySource(dashboard(), { error: 'Could not refresh' }) },
    });
    expect(screen.getByText('Last verified · refresh failed')).toBeInTheDocument();
    expect(screen.getByText('92.0%')).toBeInTheDocument();
  });

  it('forces a refresh from the error state', () => {
    const { refresh } = renderWithDashboardContext(<DeliveryKpisWidget />, {
      sources: { deliveryKpis: readySource(null, { error: 'Could not refresh' }) },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(refresh).toHaveBeenCalledWith(true);
  });

  it('is not available on a 403', () => {
    renderWithDashboardContext(<DeliveryKpisWidget />, {
      sources: { deliveryKpis: readySource(null, { forbidden: true }) },
    });
    expect(screen.getByText('Not available for your role')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('omits the analytics link when it cannot be opened', () => {
    renderWithDashboardContext(<DeliveryKpisWidget />, {
      sources: { deliveryKpis: readySource(dashboard()) },
      env: { canOpen: () => false },
    });
    expect(screen.getByText('92.0%')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });
});
