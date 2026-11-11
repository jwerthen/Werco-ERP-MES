import React from 'react';
import { fireEvent, screen, within } from '@testing-library/react';
import { readySource, renderWithDashboardContext } from '../../../../test-utils/dashboardWidgets';
import type { QualitySummaryData } from '../../sources/coreSources';
import { NCR_MONTH_HEADING, QualityPulseWidget } from './QualityPulseWidget';

const SUMMARY: QualitySummaryData = {
  open_ncrs: 4,
  open_cars: 2,
  pending_fais: 1,
  ncr_dispositions: { rework: 3, scrap: 1, hold_for_mrb: 2 },
};

describe('QualityPulseWidget', () => {
  it('shows open findings with links and the month buckets under an accurate label', () => {
    renderWithDashboardContext(<QualityPulseWidget />, { sources: { quality: readySource(SUMMARY) } });

    expect(screen.getByRole('link', { name: /Open NCRs\s*4/ })).toHaveAttribute('href', '/quality?tab=ncr&filter=open');
    expect(screen.getByRole('link', { name: /Open CARs\s*2/ })).toHaveAttribute('href', '/quality?tab=car');
    expect(screen.getByRole('link', { name: /Pending FAIs\s*1/ })).toHaveAttribute('href', '/quality?tab=fai');

    const heading = screen.getByRole('heading', { name: NCR_MONTH_HEADING });
    expect(heading).toHaveAttribute('title', expect.stringMatching(/UTC/));
    const section = heading.closest('section') as HTMLElement;
    expect(within(section).getByText('6 total')).toBeInTheDocument();
    // `pending` is always shown, first, even when the server sent no pending bucket.
    const buckets = within(section).getAllByRole('listitem');
    expect(buckets[0]).toHaveTextContent('Pending0');
    expect(within(section).getByText('Rework').nextSibling).toHaveTextContent('3');
    expect(within(section).getByText('Scrap').nextSibling).toHaveTextContent('1');
    // A disposition this bundle does not know is still counted, never dropped.
    expect(within(section).getByText('Hold for mrb').nextSibling).toHaveTextContent('2');
  });

  it('renders the empty state when there is nothing open and no NCRs this month', () => {
    renderWithDashboardContext(<QualityPulseWidget />, {
      sources: { quality: readySource({ open_ncrs: 0, open_cars: 0, pending_fais: 0, ncr_dispositions: {} }) },
    });
    expect(screen.getByText('No open quality findings')).toBeInTheDocument();
  });

  it('tolerates a summary without dispositions', () => {
    renderWithDashboardContext(<QualityPulseWidget />, {
      sources: { quality: readySource({ open_ncrs: 1, open_cars: 0, pending_fais: 0 } as QualitySummaryData) },
    });
    expect(screen.getByText('0 total')).toBeInTheDocument();
  });

  it('keeps last verified data when a refresh fails', () => {
    renderWithDashboardContext(<QualityPulseWidget />, {
      sources: { quality: readySource(SUMMARY, { error: 'Could not refresh' }) },
    });
    expect(screen.getByText('Last verified · refresh failed')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: NCR_MONTH_HEADING })).toBeInTheDocument();
  });

  it('forces a refresh from the error state', () => {
    const { refresh } = renderWithDashboardContext(<QualityPulseWidget />, {
      sources: { quality: readySource(null, { error: 'Could not refresh' }) },
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Quality summary is unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(refresh).toHaveBeenCalledWith(true);
  });

  it('is not available on a 403', () => {
    renderWithDashboardContext(<QualityPulseWidget />, {
      sources: { quality: readySource(null, { forbidden: true }) },
    });
    expect(screen.getByText('Not available for your role')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('renders the counts without links when the viewer cannot open Quality', () => {
    renderWithDashboardContext(<QualityPulseWidget />, {
      sources: { quality: readySource(SUMMARY) },
      env: { canOpen: () => false },
    });
    expect(screen.getByText('Open NCRs')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('reads as loading before the quality source arrives', () => {
    renderWithDashboardContext(<QualityPulseWidget />);
    expect(screen.getByRole('status')).toHaveTextContent('Loading quality summary…');
  });
});
