/**
 * JobWall + JobTile — the work-order wall (owner feedback 2026-07-15: WOs
 * with their current operation, not machines). Locks: SERVER order preserved
 * (never re-sorted client-side), the designed "No open work orders" empty
 * state, "+N more" from the uncapped jobs_total, the five state words with
 * DOWN > BLOCKED > LATE > RUNNING > WAITING precedence, tile anatomy (part /
 * WO qty / "Op n/total · name · work center" / crew suffix / elapsed only
 * while running / LATE chip / down context line), flash keyed by WO number,
 * and optional-field safety against a sparse payload.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import type { WallboardJob } from '../../types/wallboard';
import JobWall from './JobWall';

function job(overrides: Partial<WallboardJob> & { wo_number: string }): WallboardJob {
  return {
    part_number: `PN-${overrides.wo_number}`,
    status: 'in_progress',
    qty_complete: 0,
    qty_ordered: 10,
    promise_date: null,
    is_late: false,
    days_late: 0,
    blocked: false,
    down: false,
    running: false,
    ops_completed: 0,
    ops_total: 3,
    current_op: {
      sequence: 10,
      name: 'Cut',
      work_center_code: 'SAW-1',
      work_center_name: 'Saw 1',
      status: 'ready',
      qty_done: 0,
      qty_target: 10,
      crew: [],
      crew_count: 0,
      elapsed_minutes: 0,
    },
    ...overrides,
  };
}

function renderWall(
  jobs: WallboardJob[],
  {
    jobsTotal = null,
    flashKeys = new Set<string>(),
    extraMinutes = 0,
  }: { jobsTotal?: number | null; flashKeys?: Set<string>; extraMinutes?: number } = {}
) {
  return render(
    <JobWall jobs={jobs} jobsTotal={jobsTotal} pollKey="poll-0" flashKeys={flashKeys} extraMinutes={extraMinutes} />
  );
}

describe('JobWall', () => {
  it('renders the designed empty state when no work orders are open', () => {
    renderWall([]);
    expect(screen.getByTestId('job-wall')).toHaveTextContent('No open work orders');
    expect(screen.queryByTestId('wallboard-grid')).not.toBeInTheDocument();
  });

  it('preserves SERVER order — never re-sorts client-side', () => {
    // Deliberately "wrong" alarm order: running first, down second. The wall
    // must render exactly this order — priority sorting is the server's job.
    renderWall([job({ wo_number: 'WO-2', running: true }), job({ wo_number: 'WO-1', down: true })]);
    const ids = screen.getAllByTestId(/^job-tile-WO/).map(el => el.getAttribute('data-testid'));
    expect(ids).toEqual(['job-tile-WO-2', 'job-tile-WO-1']);
  });

  it('shows "+N more" from the uncapped jobs_total, and omits it when nothing is hidden', () => {
    const { unmount } = renderWall([job({ wo_number: 'WO-1' })], { jobsTotal: 26 });
    expect(screen.getByTestId('job-wall')).toHaveTextContent('+25 more work orders');
    unmount();

    // jobs_total equal to the tile count (or absent on a sparse payload) → no line.
    renderWall([job({ wo_number: 'WO-1' })], { jobsTotal: 1 });
    expect(screen.getByTestId('job-wall')).not.toHaveTextContent('more work orders');
  });

  it('flashes only the tiles whose job:{wo}:{class} key is in flashKeys', () => {
    renderWall([job({ wo_number: 'WO-1', down: true }), job({ wo_number: 'WO-2', blocked: true })], {
      flashKeys: new Set(['job:WO-2:blocked']),
    });
    expect(screen.getByTestId('job-tile-header-WO-2').className).toContain('wb-flash-new');
    expect(screen.getByTestId('job-tile-header-WO-1').className).not.toContain('wb-flash-new');
  });
});

describe('JobTile', () => {
  it('applies the state word with DOWN > BLOCKED > LATE > RUNNING > WAITING precedence', () => {
    renderWall([
      job({ wo_number: 'WO-1', down: true, blocked: true, is_late: true, running: true }),
      job({ wo_number: 'WO-2', blocked: true, is_late: true, running: true }),
      job({ wo_number: 'WO-3', is_late: true, running: true }),
      job({ wo_number: 'WO-4', running: true }),
      job({ wo_number: 'WO-5' }),
    ]);
    expect(screen.getByTestId('job-tile-header-WO-1')).toHaveTextContent('DOWN');
    expect(screen.getByTestId('job-tile-header-WO-2')).toHaveTextContent('BLOCKED');
    expect(screen.getByTestId('job-tile-header-WO-3')).toHaveTextContent('LATE');
    expect(screen.getByTestId('job-tile-header-WO-4')).toHaveTextContent('RUNNING');
    expect(screen.getByTestId('job-tile-header-WO-5')).toHaveTextContent('WAITING');
  });

  it('renders the full anatomy: WO band, part, WO qty, op line with crew, live elapsed', () => {
    renderWall(
      [
        job({
          wo_number: 'WO-9',
          part_number: 'PN-88',
          qty_complete: 12,
          qty_ordered: 50,
          running: true,
          ops_completed: 2,
          ops_total: 5,
          current_op: {
            sequence: 30,
            name: 'Mill',
            work_center_code: 'MILL-1',
            work_center_name: 'Mill 1',
            status: 'in_progress',
            qty_done: 12,
            qty_target: 50,
            crew: ['Jon W.', 'Sam K.'],
            crew_count: 3,
            elapsed_minutes: 75,
          },
        }),
      ],
      { extraMinutes: 5 } // elapsed ticks client-side between polls
    );
    const tile = screen.getByTestId('job-tile-WO-9');
    expect(screen.getByTestId('job-tile-header-WO-9')).toHaveTextContent('WO-9');
    expect(tile).toHaveTextContent('PN-88');
    expect(tile).toHaveTextContent('12/50');
    expect(tile).toHaveTextContent('Op 3/5 · Mill · Mill 1 · Jon W. +2');
    expect(tile).toHaveTextContent('1h20m');
  });

  it('shows the LATE chip with days and no elapsed on a non-running late job', () => {
    renderWall([job({ wo_number: 'WO-7', is_late: true, days_late: 4 })], { extraMinutes: 9 });
    const tile = screen.getByTestId('job-tile-WO-7');
    expect(tile).toHaveTextContent('Late 4d');
    // Not running → no open labor → no elapsed counter (extraMinutes must not
    // invent one on a waiting/late job).
    expect(tile).not.toHaveTextContent('9m');
  });

  it('leads a DOWN tile body with the machine-down context', () => {
    renderWall([job({ wo_number: 'WO-6', down: true })]);
    expect(screen.getByTestId('job-tile-WO-6')).toHaveTextContent('Machine Down · Saw 1');
  });

  it('survives a sparse job (every optional field absent)', () => {
    renderWall([{ wo_number: 'WO-MIN' }]);
    const tile = screen.getByTestId('job-tile-WO-MIN');
    expect(screen.getByTestId('job-tile-header-WO-MIN')).toHaveTextContent('WAITING');
    // No part, no current op → em-dashes; zeroed WO qty.
    expect(tile).toHaveTextContent('—');
    expect(tile).toHaveTextContent('0/0');
  });
});
