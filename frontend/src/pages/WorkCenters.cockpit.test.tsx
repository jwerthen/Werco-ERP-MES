/**
 * WorkCenters — instrument-panel render lock.
 *
 * The page keeps the cockpit aesthetic — a top MiniStatStrip of aggregate
 * status counts (Total / Available / In Use / Maintenance / Offline) — but the
 * per-type panels were migrated onto a single grouped <DataTable>: rows are
 * grouped by work-center type (curated order) with section headers + counts,
 * the columns sort within each group, and each row carries an inline status
 * <select> + an edit action. This guards that the aggregate strip computes the
 * right counts, that the type groups render with their counts, and that a row
 * renders its data with an interactive status select wired to
 * updateWorkCenterStatus.
 *
 * The desktop table and the responsive mobile cards both mount in JSDOM (the
 * `hidden md:block` / `md:hidden` CSS isn't applied), so the table-specific
 * assertions scope into the `data-table` element to avoid the duplicate
 * code/name nodes the mobile cards also render.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import WorkCenters from './WorkCenters';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkCenters: jest.fn(),
    getWorkCenterTypes: jest.fn(),
    updateWorkCenterStatus: jest.fn(),
    createWorkCenter: jest.fn(),
    updateWorkCenter: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const baseWc = {
  version: 0,
  description: '',
  availability_rate: 1,
  is_active: true,
  building: '',
  area: '',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

// Five work centers across two types, with a spread of statuses so the
// aggregate counts are non-trivial: 2 available, 1 in_use, 1 maintenance, 1 offline.
const workCenters = [
  { ...baseWc, id: 1, code: 'LASER-01', name: 'Trumpf Fiber', work_center_type: 'laser', hourly_rate: 120, capacity_hours_per_day: 16, efficiency_factor: 0.9, current_status: 'available' },
  { ...baseWc, id: 2, code: 'LASER-02', name: 'Amada', work_center_type: 'laser', hourly_rate: 110, capacity_hours_per_day: 8, efficiency_factor: 0.95, current_status: 'in_use' },
  { ...baseWc, id: 3, code: 'WELD-01', name: 'TIG Cell A', work_center_type: 'welding', hourly_rate: 85, capacity_hours_per_day: 8, efficiency_factor: 1, current_status: 'available' },
  { ...baseWc, id: 4, code: 'WELD-02', name: 'TIG Cell B', work_center_type: 'welding', hourly_rate: 85, capacity_hours_per_day: 8, efficiency_factor: 1, current_status: 'maintenance' },
  { ...baseWc, id: 5, code: 'WELD-03', name: 'MIG Cell', work_center_type: 'welding', hourly_rate: 80, capacity_hours_per_day: 8, efficiency_factor: 1, current_status: 'offline' },
];

function renderPage() {
  return render(
    <MemoryRouter>
      <WorkCenters />
    </MemoryRouter>
  );
}

describe('WorkCenters cockpit overhaul', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getWorkCenters.mockResolvedValue(workCenters as any);
    mockedApi.getWorkCenterTypes.mockResolvedValue({ types: ['laser', 'welding'] } as any);
    mockedApi.updateWorkCenterStatus.mockResolvedValue({} as any);
  });

  it('shows the aggregate MiniStat status strip with correct counts', async () => {
    renderPage();

    // Wait for the initial data load (group headers + rows render).
    await screen.findAllByTestId('group-header');

    // Status words like "Available"/"In Use" also appear in the per-row status
    // <select> options, so scope the label lookup to the MiniStat tile by its
    // `stat-label` class (only KPI tiles use it). Then assert the count sits in
    // the same tile.
    const expectStat = (label: string, value: string) => {
      const labelEl = screen
        .getAllByText(label)
        .find((el) => el.classList.contains('stat-label'));
      expect(labelEl).toBeDefined();
      const tile = (labelEl as HTMLElement).closest('div.card');
      expect(tile).not.toBeNull();
      expect(within(tile as HTMLElement).getByText(value)).toBeInTheDocument();
    };

    expectStat('Total', '5');
    expectStat('Available', '2');
    expectStat('In Use', '1');
    expectStat('Maintenance', '1');
    expectStat('Offline', '1');
  });

  it('groups rows by work-center type with section headers + counts', async () => {
    renderPage();

    // Group-header rows only appear once data loads (they live inside the
    // desktop table). findAllByTestId waits for them.
    const headerRows = await screen.findAllByTestId('group-header');
    const headers = headerRows.map((tr) => tr.textContent || '');

    // Two type groups: Laser (2 centers) and Welding (3 centers).
    expect(headers.some((h) => h.includes('Laser') && h.includes('2 centers'))).toBe(true);
    expect(headers.some((h) => h.includes('Welding') && h.includes('3 centers'))).toBe(true);
  });

  it('orders the type groups by workCenterTypeOrder (laser before welding)', async () => {
    renderPage();

    // The mocked getWorkCenterTypes returns ['laser', 'welding'], which the page
    // feeds to the DataTable as the curated group `order`. So Laser must render
    // its header BEFORE Welding regardless of row insertion order.
    const headerRows = await screen.findAllByTestId('group-header');
    const headers = headerRows.map((tr) => tr.textContent || '');
    const laserIdx = headers.findIndex((h) => h.includes('Laser'));
    const weldingIdx = headers.findIndex((h) => h.includes('Welding'));
    expect(laserIdx).toBeGreaterThanOrEqual(0);
    expect(weldingIdx).toBeGreaterThanOrEqual(0);
    expect(laserIdx).toBeLessThan(weldingIdx);
  });

  it('places each row under its own type group header', async () => {
    renderPage();

    await screen.findAllByTestId('group-header');
    const table = screen.getByTestId('data-table');
    // Walk the body rows in DOM order, tracking the most recent group header so
    // every data row is attributed to the group it visually sits under.
    const bodyRows = within(table).getAllByRole('row').slice(1); // drop the sort header row
    const rowGroup: Record<string, string> = {};
    let currentGroup = '';
    bodyRows.forEach((tr) => {
      const headerCell = tr.querySelector('td[colspan]');
      if (headerCell) {
        currentGroup = (headerCell.textContent || '').includes('Laser') ? 'laser' : 'welding';
        return;
      }
      const code = within(tr).getAllByRole('cell')[0].textContent || '';
      if (code) rowGroup[code] = currentGroup;
    });

    // Laser centers under Laser; welding centers under Welding.
    expect(rowGroup['LASER-01']).toBe('laser');
    expect(rowGroup['LASER-02']).toBe('laser');
    expect(rowGroup['WELD-01']).toBe('welding');
    expect(rowGroup['WELD-02']).toBe('welding');
    expect(rowGroup['WELD-03']).toBe('welding');
  });

  it('sorts WITHIN a type group when a sortable column header is clicked, keeping group order fixed', async () => {
    renderPage();

    await screen.findAllByTestId('group-header');
    const table = screen.getByTestId('data-table');

    // Helper: ordered list of {group | code} as it appears in the DOM, scoped to
    // the desktop table (mobile cards duplicate codes in JSDOM).
    const layout = () => {
      const bodyRows = within(table).getAllByRole('row').slice(1);
      return bodyRows.map((tr) => {
        const headerCell = tr.querySelector('td[colspan]');
        if (headerCell) {
          return { group: (headerCell.textContent || '').includes('Laser') ? 'laser' : 'welding' };
        }
        return { code: within(tr).getAllByRole('cell')[0].textContent || '' };
      });
    };

    // Sort by Rate/hr ascending. Within Welding: WELD-03 ($80) < WELD-01/02 ($85).
    // Within Laser: LASER-02 ($110) < LASER-01 ($120). Group order stays laser→welding.
    const rateHeader = within(table).getByRole('button', { name: /Rate\/hr/i });
    fireEvent.click(rateHeader);

    const asc = layout();
    // Group order unchanged: laser group renders before welding group.
    const laserGroupIdx = asc.findIndex((e) => 'group' in e && e.group === 'laser');
    const weldGroupIdx = asc.findIndex((e) => 'group' in e && e.group === 'welding');
    expect(laserGroupIdx).toBeLessThan(weldGroupIdx);

    // Codes between the welding header and the end, in order, cheapest first.
    const weldCodes = asc
      .slice(weldGroupIdx + 1)
      .filter((e): e is { code: string } => 'code' in e)
      .map((e) => e.code);
    expect(weldCodes).toEqual(['WELD-03', 'WELD-01', 'WELD-02']);

    // Laser codes (between the two group headers), cheapest first.
    const laserCodes = asc
      .slice(laserGroupIdx + 1, weldGroupIdx)
      .filter((e): e is { code: string } => 'code' in e)
      .map((e) => e.code);
    expect(laserCodes).toEqual(['LASER-02', 'LASER-01']);

    // Toggle to descending — within-group order flips, group order still fixed.
    fireEvent.click(rateHeader);
    const desc = layout();
    const weldDescIdx = desc.findIndex((e) => 'group' in e && e.group === 'welding');
    const laserDescIdx = desc.findIndex((e) => 'group' in e && e.group === 'laser');
    expect(laserDescIdx).toBeLessThan(weldDescIdx);
    const laserDescCodes = desc
      .slice(laserDescIdx + 1, weldDescIdx)
      .filter((e): e is { code: string } => 'code' in e)
      .map((e) => e.code);
    expect(laserDescCodes).toEqual(['LASER-01', 'LASER-02']);
  });

  it('renders a work-center row with code + rate + capacity and an inline status select', async () => {
    renderPage();

    // Wait for the data load (group headers appear), then scope to the desktop
    // table — the mobile cards duplicate code/name in JSDOM.
    await screen.findAllByTestId('group-header');
    const table = screen.getByTestId('data-table');
    const codeCell = within(table).getByText('LASER-01');
    const row = codeCell.closest('tr');
    expect(row).not.toBeNull();
    const r = within(row as HTMLElement);

    // Code + name.
    expect(r.getByText('LASER-01')).toBeInTheDocument();
    expect(r.getByText('Trumpf Fiber')).toBeInTheDocument();

    // Dense numeric columns: rate/hr ($), capacity (h), efficiency factor.
    expect(r.getByText('$120')).toBeInTheDocument();
    expect(r.getByText('16h')).toBeInTheDocument();
    expect(r.getByText('0.90')).toBeInTheDocument();

    // Inline status <select> reflects the current status.
    const statusSelect = r.getByRole('combobox') as HTMLSelectElement;
    expect(statusSelect.value).toBe('available');
  });

  it('wires the inline status select to updateWorkCenterStatus', async () => {
    renderPage();

    await screen.findAllByTestId('group-header');
    const table = screen.getByTestId('data-table');
    const row = within(table).getByText('LASER-01').closest('tr') as HTMLElement;
    const statusSelect = within(row).getByRole('combobox');

    fireEvent.change(statusSelect, { target: { value: 'maintenance' } });

    await waitFor(() => {
      expect(mockedApi.updateWorkCenterStatus).toHaveBeenCalledWith(1, 'maintenance');
    });
  });

  it('dims inactive (decommissioned) work centers', async () => {
    // The page loads inactive centers (active_only=false); they must stay
    // visually distinguishable as the old per-type panels dimmed them.
    mockedApi.getWorkCenters.mockResolvedValue([
      ...workCenters,
      {
        ...baseWc,
        id: 6,
        code: 'OLD-01',
        name: 'Retired Press',
        work_center_type: 'welding',
        hourly_rate: 50,
        capacity_hours_per_day: 8,
        efficiency_factor: 1,
        current_status: 'offline',
        is_active: false,
      },
    ] as any);
    renderPage();

    await screen.findAllByTestId('group-header');
    const table = screen.getByTestId('data-table');
    const inactiveRow = within(table).getByText('OLD-01').closest('tr') as HTMLElement;
    const activeRow = within(table).getByText('LASER-01').closest('tr') as HTMLElement;
    expect(inactiveRow).toHaveClass('opacity-60');
    expect(activeRow).not.toHaveClass('opacity-60');
  });
});
