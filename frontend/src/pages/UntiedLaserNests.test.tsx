/**
 * Untied Laser Nests — the office worklist + sheet report.
 *
 * Pins: the reads are the only thing on load (no write), "Tie this sheet" posts
 * the row's proposed_tie through the existing tie verb and refetches (never
 * optimistic), a refusal is shown verbatim with the dialog left open, an
 * ambiguous match needs the planner's explicit pick, and the sheet report starts
 * a cycle count through the existing endpoint (or links the open one).
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import api from '../services/api';
import UntiedLaserNests from './UntiedLaserNests';
import { ToastProvider } from '../components/ui';
import type {
  NestTieCoverageResponse,
  SheetCandidate,
  UntiedNestListResponse,
  UntiedNestRow,
  UntiedSheetReportResponse,
} from '../types/untiedNests';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getUntiedLaserNests: jest.fn(),
    getLaserNestTieCoverage: jest.fn(),
    getUntiedSheetReport: jest.fn(),
    getWorkCenters: jest.fn(),
    tieNestSheet: jest.fn(),
    createCycleCount: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const SHEET: SheetCandidate = {
  part_id: 501,
  part_number: 'SHT-A36-250',
  part_name: 'A36 plate 0.250 60x120',
  unit_of_measure: 'sheets',
  score: 100,
  reason: 'Exact thickness, grade and size.',
  basis: 'deterministic',
  on_hand: 12,
  on_hand_known: true,
};
const SHEET_ALT: SheetCandidate = { ...SHEET, part_id: 502, part_number: 'SHT-A36-250-B', score: 80 };

function row(overrides: Partial<UntiedNestRow> = {}): UntiedNestRow {
  return {
    laser_nest_id: 11,
    work_order_operation_id: 901,
    operation_number: '10',
    operation_name: 'Laser',
    operation_status: 'ready',
    work_order_id: 77,
    work_order_number: 'WO-0077',
    work_order_status: 'released',
    parent_work_order_id: null,
    parent_work_order_number: null,
    work_center_id: 3,
    work_center_name: 'Laser 1',
    nest_name: 'Nest 1',
    cnc_number: 'CNC-1001',
    material: 'A36',
    thickness: '0.250',
    sheet_size: '60 x 120',
    sheet_descriptor: 'A36 / 0.250 / 60 x 120',
    planned_runs: 4,
    runs_complete: 0,
    runs_scrapped: 0,
    completed_at: null,
    is_completed: false,
    job_level_tie_count: 0,
    tieable: true,
    tie_blocked_reason: null,
    suggestion: { status: 'matched', candidate: SHEET, candidates: [SHEET], diagnostic: null },
    proposed_tie: {
      work_order_id: 77,
      part_id: 501,
      work_order_operation_id: 901,
      source: 'nest',
      qty_per_run: 1,
      qty_planned: 4,
    },
    ...overrides,
  };
}

function listOf(items: UntiedNestRow[]): UntiedNestListResponse {
  return {
    items,
    total: items.length,
    limit: 50,
    offset: 0,
    completed_from: '2026-08-25T05:00:00Z',
    completed_to: '2026-11-24T06:00:00Z',
    matcher_available: true,
  };
}

const COVERAGE: NestTieCoverageResponse = {
  months: [
    { month: '2026-10', completed_nests: 10, tied_nests: 4, untied_nests: 6, tied_percent: 40 },
    { month: '2026-11', completed_nests: 0, tied_nests: 0, untied_nests: 0, tied_percent: null },
  ],
  completed_nests: 10,
  tied_nests: 4,
  tied_percent: 40,
};

function renderPage(url = '/laser-nests/untied') {
  return render(
    <ToastProvider>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route path="/laser-nests/untied" element={<UntiedLaserNests />} />
        </Routes>
      </MemoryRouter>
    </ToastProvider>
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getLaserNestTieCoverage.mockResolvedValue(COVERAGE);
  mockedApi.getWorkCenters.mockResolvedValue([]);
});

describe('UntiedLaserNests — worklist', () => {
  it('lists untied nests and the monthly coverage, writing nothing on load', async () => {
    mockedApi.getUntiedLaserNests.mockResolvedValue(listOf([row()]));
    renderPage();

    expect(await screen.findByText('CNC-1001')).toBeInTheDocument();
    expect(screen.getByText('A36 / 0.250 / 60 x 120')).toBeInTheDocument();
    expect(screen.getByText('SHT-A36-250')).toBeInTheDocument();
    expect(screen.getByTestId('nest-coverage-total')).toHaveTextContent('40%');
    expect(screen.getByTestId('nest-coverage-2026-11')).toHaveTextContent('—');
    expect(mockedApi.getUntiedLaserNests).toHaveBeenCalledWith(
      expect.objectContaining({ status: 'all', limit: 50, offset: 0 })
    );
    expect(mockedApi.tieNestSheet).not.toHaveBeenCalled();
    expect(mockedApi.createCycleCount).not.toHaveBeenCalled();
  });

  it('passes URL filters through to the read', async () => {
    mockedApi.getUntiedLaserNests.mockResolvedValue(listOf([]));
    renderPage('/laser-nests/untied?status=completed&work_order_id=77&from=2026-10-01&to=2026-10-31&page=2');
    await waitFor(() =>
      expect(mockedApi.getUntiedLaserNests).toHaveBeenCalledWith({
        status: 'completed',
        work_order_id: 77,
        work_center_id: undefined,
        completed_from: '2026-10-01',
        completed_to: '2026-10-31',
        limit: 50,
        offset: 50,
      })
    );
  });

  it('ties the matched sheet through the existing tie verb, then re-reads the list', async () => {
    mockedApi.getUntiedLaserNests.mockResolvedValueOnce(listOf([row()])).mockResolvedValue(listOf([]));
    mockedApi.tieNestSheet.mockResolvedValue({} as never);
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: /tie this sheet/i }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /tie this sheet/i }));

    await waitFor(() => expect(mockedApi.tieNestSheet).toHaveBeenCalledTimes(1));
    expect(mockedApi.tieNestSheet).toHaveBeenCalledWith({
      work_order_id: 77,
      part_id: 501,
      work_order_operation_id: 901,
      source: 'nest',
      qty_per_run: 1,
      qty_planned: 4,
    });
    await waitFor(() => expect(mockedApi.getUntiedLaserNests).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('shows a refusal verbatim and keeps the dialog open', async () => {
    const refusal =
      'Part SHT-A36-250 is already tied to this operation (allocation 9). Edit or untie that allocation instead.';
    mockedApi.getUntiedLaserNests.mockResolvedValue(listOf([row()]));
    mockedApi.tieNestSheet.mockRejectedValue({ response: { status: 409, data: { detail: refusal } } });
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: /tie this sheet/i }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /tie this sheet/i }));

    expect(await within(dialog).findByTestId('tie-nest-sheet-error')).toHaveTextContent(refusal);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(mockedApi.getUntiedLaserNests).toHaveBeenCalledTimes(1);
  });

  it('warns about a job-level tie before tying the nest as well', async () => {
    mockedApi.getUntiedLaserNests.mockResolvedValue(listOf([row({ job_level_tie_count: 1 })]));
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: /tie this sheet/i }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('alert')).toHaveTextContent(/draw it twice/i);
  });

  it('makes the planner pick a shortlist part for an ambiguous match', async () => {
    mockedApi.getUntiedLaserNests.mockResolvedValue(
      listOf([
        row({
          suggestion: {
            status: 'ambiguous',
            candidate: null,
            candidates: [SHEET, SHEET_ALT],
            diagnostic: 'Two parts fit.',
          },
          proposed_tie: null,
        }),
      ])
    );
    mockedApi.tieNestSheet.mockResolvedValue({} as never);
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: /choose sheet/i }));
    const dialog = await screen.findByRole('dialog');
    const submit = within(dialog).getByRole('button', { name: /tie this sheet/i });
    expect(submit).toBeDisabled();

    fireEvent.click(within(dialog).getByLabelText(/SHT-A36-250-B/));
    fireEvent.click(submit);
    await waitFor(() =>
      expect(mockedApi.tieNestSheet).toHaveBeenCalledWith({
        work_order_id: 77,
        part_id: 502,
        work_order_operation_id: 901,
        source: 'nest',
        qty_per_run: 1,
        qty_planned: 4,
      })
    );
  });

  it('offers no tie on a finished job and says why', async () => {
    const reason = 'Work order WO-0077 is complete; a sheet can no longer be tied to it.';
    mockedApi.getUntiedLaserNests.mockResolvedValue(
      listOf([row({ tieable: false, tie_blocked_reason: reason, proposed_tie: null, is_completed: true })])
    );
    renderPage();
    expect(await screen.findByText(reason)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /tie this sheet/i })).not.toBeInTheDocument();
  });
});

describe('UntiedLaserNests — sheet report', () => {
  const REPORT: UntiedSheetReportResponse = {
    groups: [
      {
        spec_key: 'a36|0.250|60x120',
        material: 'A36',
        thickness: '0.250',
        sheet_size: '60 x 120',
        sheet_descriptor: 'A36 / 0.250 / 60 x 120',
        nest_count: 3,
        sheets_burned: 9,
        work_order_count: 2,
        work_order_numbers: ['WO-0077', 'WO-0078'],
        first_completed_at: '2026-10-02T15:00:00Z',
        last_completed_at: '2026-10-20T15:00:00Z',
        suggestion: { status: 'matched', candidate: SHEET, candidates: [SHEET], diagnostic: null },
        open_cycle_counts: [],
      },
      {
        spec_key: 'a36|0.125|48x96',
        material: 'A36',
        thickness: '0.125',
        sheet_size: '48 x 96',
        sheet_descriptor: 'A36 / 0.125 / 48 x 96',
        nest_count: 1,
        sheets_burned: 2,
        work_order_count: 1,
        work_order_numbers: ['WO-0079'],
        first_completed_at: '2026-10-05T15:00:00Z',
        last_completed_at: '2026-10-05T15:00:00Z',
        suggestion: { status: 'matched', candidate: SHEET_ALT, candidates: [SHEET_ALT], diagnostic: null },
        open_cycle_counts: [
          { id: 33, count_number: 'CC-20261101-001', status: 'scheduled', scheduled_date: '2026-11-01' },
        ],
      },
    ],
    nest_count: 4,
    sheets_burned: 11,
    completed_from: '2026-08-25T05:00:00Z',
    completed_to: '2026-11-24T06:00:00Z',
    truncated: false,
    matcher_available: true,
  };

  it('groups sheets, links an open count, and starts a new one through the existing endpoint', async () => {
    mockedApi.getUntiedSheetReport.mockResolvedValue(REPORT);
    mockedApi.createCycleCount.mockResolvedValue({ id: 40, count_number: 'CC-20261123-001' });
    renderPage('/laser-nests/untied?view=sheets');

    expect(await screen.findByText(/grouping is approximate/i)).toBeInTheDocument();
    expect(mockedApi.getUntiedLaserNests).not.toHaveBeenCalled();
    expect(screen.getByRole('link', { name: /CC-20261101-001/ })).toHaveAttribute(
      'href',
      '/inventory?inventory_tab=counts&cycle_count=33'
    );

    const buttons = screen.getAllByRole('button', { name: /start cycle count/i });
    expect(buttons).toHaveLength(1);
    fireEvent.click(buttons[0]);
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /start cycle count/i }));

    await waitFor(() => expect(mockedApi.createCycleCount).toHaveBeenCalledTimes(1));
    expect(mockedApi.createCycleCount).toHaveBeenCalledWith(
      expect.objectContaining({ part_id: 501, scheduled_date: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/) })
    );
    await waitFor(() => expect(mockedApi.getUntiedSheetReport).toHaveBeenCalledTimes(2));
  });

  it('shows a cycle-count refusal verbatim and keeps the dialog open', async () => {
    mockedApi.getUntiedSheetReport.mockResolvedValue(REPORT);
    mockedApi.createCycleCount.mockRejectedValue({ response: { status: 404, data: { detail: 'Part not found' } } });
    renderPage('/laser-nests/untied?view=sheets');

    fireEvent.click((await screen.findAllByRole('button', { name: /start cycle count/i }))[0]);
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /start cycle count/i }));

    expect(await within(dialog).findByTestId('start-sheet-count-error')).toHaveTextContent('Part not found');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(mockedApi.getUntiedSheetReport).toHaveBeenCalledTimes(1);
  });

  it('says when the report was cut short and marks the total as a floor', async () => {
    mockedApi.getUntiedSheetReport.mockResolvedValue({ ...REPORT, truncated: true });
    renderPage('/laser-nests/untied?view=sheets');
    expect(await screen.findByTestId('untied-sheet-report-truncated')).toBeInTheDocument();
    expect(screen.getByText('≥ 11')).toBeInTheDocument();
  });
});

describe('UntiedLaserNests — load failures', () => {
  it('shows the worklist error with a working Retry', async () => {
    mockedApi.getUntiedLaserNests
      .mockRejectedValueOnce({ response: { status: 500, data: { detail: 'Server exploded' } } })
      .mockResolvedValue(listOf([row()]));
    renderPage();

    const retry = await screen.findByRole('button', { name: /retry/i });
    fireEvent.click(retry);
    expect(await screen.findByText('CNC-1001')).toBeInTheDocument();
    expect(mockedApi.getUntiedLaserNests).toHaveBeenCalledTimes(2);
    expect(mockedApi.tieNestSheet).not.toHaveBeenCalled();
  });
});
