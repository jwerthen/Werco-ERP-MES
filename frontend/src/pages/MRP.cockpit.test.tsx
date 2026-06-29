/**
 * MRP cockpit overhaul — render-correctness guard.
 *
 * The MRP page was rebuilt into the instrument-panel cockpit: a MiniStat KPI
 * strip up top, and a shared one-line `ActionRow` that renders one MRP action.
 * The de-dup that matters: the SAME action data feeds both the Material
 * Shortages panel and the Run Details panel, rendered through that single
 * ActionRow component and cross-linked by the stable `action_id`/`id`.
 *
 * This test locks:
 *  1. The MiniStat strip renders its KPI tiles (labels + values from the
 *     loaded shortages/latest-run data).
 *  2. The shared ActionRow renders for an MRP action in the Shortages panel,
 *     and — after selecting the run — the same action (same id) renders again
 *     in the Run Details panel, carrying a `data-action-id` cross-link.
 *
 * The page only calls api.getMRPRuns() + api.getMRPShortages() on mount;
 * api.getMRPActions(runId) fires when a run row is clicked.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import MRPPage from './MRP';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getMRPRuns: jest.fn(),
    getMRPShortages: jest.fn(),
    getMRPActions: jest.fn(),
    runMRP: jest.fn(),
    processMRPAction: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

// Same stable action id shared by the shortage and the run action — this is
// the cross-link that proves both panels render the same action data.
const SHARED_ACTION_ID = 501;

const latestRun = {
  id: 9,
  run_number: 'MRP-2026-009',
  planning_horizon_days: 90,
  status: 'complete' as const,
  started_at: '2026-06-28T10:00:00Z',
  completed_at: '2026-06-28T10:05:00Z',
  total_parts_analyzed: 42,
  total_requirements: 17,
  total_actions: 8,
  created_at: '2026-06-28T10:00:00Z',
};

const shortagesSummary = {
  mrp_run_id: 9,
  mrp_run_number: 'MRP-2026-009',
  run_date: '2026-06-28T10:05:00Z',
  total_shortages: 1,
  expedite_count: 1,
  shortages: [
    {
      action_id: SHARED_ACTION_ID,
      part_id: 100,
      part_number: 'PN-SHORT-1',
      part_name: 'Titanium Bracket',
      action_type: 'expedite',
      quantity: 25,
      required_date: '2026-07-15T00:00:00Z',
      order_by_date: '2026-07-01T00:00:00Z',
      priority: 1,
      is_expedite: true,
    },
  ],
};

const runActions = [
  {
    id: SHARED_ACTION_ID,
    part_id: 100,
    part: { id: 100, part_number: 'PN-SHORT-1', name: 'Titanium Bracket', part_type: 'raw_material' },
    action_type: 'expedite',
    priority: 1,
    quantity: 25,
    required_date: '2026-07-15T00:00:00Z',
    suggested_order_date: '2026-07-01T00:00:00Z',
    is_processed: false,
  },
];

function renderMRP() {
  return render(
    <MemoryRouter>
      <MRPPage />
    </MemoryRouter>
  );
}

describe('MRP cockpit: MiniStat strip + shared ActionRow de-dup', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getMRPRuns.mockResolvedValue([latestRun]);
    mockedApi.getMRPShortages.mockResolvedValue(shortagesSummary);
    mockedApi.getMRPActions.mockResolvedValue(runActions);
  });

  it('renders the MiniStat KPI strip from the loaded run/shortage data', async () => {
    renderMRP();

    // Wait for the initial load to settle (heading is always present, but the
    // KPI values come from the resolved data).
    expect(await screen.findByRole('heading', { name: /Material Requirements Planning/i })).toBeInTheDocument();

    // KPI tiles render their labels.
    expect(screen.getByText('Total Shortages')).toBeInTheDocument();
    expect(screen.getByText('Need Expedite')).toBeInTheDocument();
    expect(screen.getByText('Parts Analyzed')).toBeInTheDocument();
    expect(screen.getByText('Requirements')).toBeInTheDocument();
    expect(screen.getByText('Actions')).toBeInTheDocument();

    // Values are sourced from the latest run, anchored to their tile label.
    const partsTile = screen.getByText('Parts Analyzed').closest('div')?.parentElement as HTMLElement;
    expect(within(partsTile).getByText('42')).toBeInTheDocument();

    const reqsTile = screen.getByText('Requirements').closest('div')?.parentElement as HTMLElement;
    expect(within(reqsTile).getByText('17')).toBeInTheDocument();
  });

  it('renders the shared ActionRow for the shortage and again in Run Details (same action_id)', async () => {
    renderMRP();

    // The shortage action renders in the Material Shortages panel via ActionRow.
    // findAll because the part number could appear once initially (shortages
    // only); the row carries the stable action id as a data attribute.
    await screen.findByText('Material Shortages');

    let rows = document.querySelectorAll(`[data-action-id="${SHARED_ACTION_ID}"]`);
    expect(rows).toHaveLength(1); // only the Shortages panel so far
    expect(within(rows[0] as HTMLElement).getByText('PN-SHORT-1')).toBeInTheDocument();
    expect(within(rows[0] as HTMLElement).getByText('Titanium Bracket')).toBeInTheDocument();

    // Select the run to load its actions into the Run Details panel.
    const runButton = screen.getByRole('button', { name: /MRP-2026-009/i });
    fireEvent.click(runButton);

    await waitFor(() => {
      expect(mockedApi.getMRPActions).toHaveBeenCalledWith(latestRun.id);
    });

    // The SAME action (same id) now renders in BOTH panels through the shared
    // ActionRow — two elements carrying the same data-action-id.
    await waitFor(() => {
      const both = document.querySelectorAll(`[data-action-id="${SHARED_ACTION_ID}"]`);
      expect(both).toHaveLength(2);
    });

    const both = document.querySelectorAll(`[data-action-id="${SHARED_ACTION_ID}"]`);
    both.forEach((row) => {
      expect(within(row as HTMLElement).getByText('PN-SHORT-1')).toBeInTheDocument();
    });
  });
});
