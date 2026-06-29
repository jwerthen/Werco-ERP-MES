/**
 * WorkCenters — instrument-panel cockpit overhaul render lock.
 *
 * The page was reworked into the cockpit aesthetic: a top MiniStatStrip of
 * aggregate status counts (Total / Available / In Use / Maintenance / Offline)
 * and per-type CockpitPanels holding a dense table where each work-center row
 * shows code + rate/cap/eff and an inline status <select>. This guards that the
 * aggregate strip computes the right counts and that a row renders its data and
 * an interactive status select wired to updateWorkCenterStatus.
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

    // Wait for the initial data load (a row renders).
    await screen.findByText('LASER-01');

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

  it('renders a work-center row with code + rate/cap/eff and an inline status select', async () => {
    renderPage();

    const codeCell = await screen.findByText('LASER-01');
    const row = codeCell.closest('tr');
    expect(row).not.toBeNull();
    const r = within(row as HTMLElement);

    // Code + name.
    expect(r.getByText('LASER-01')).toBeInTheDocument();
    expect(r.getByText('Trumpf Fiber')).toBeInTheDocument();

    // Dense numeric columns: rate ($), capacity (h), efficiency.
    expect(r.getByText('$120')).toBeInTheDocument();
    expect(r.getByText('16h')).toBeInTheDocument();
    expect(r.getByText('0.9')).toBeInTheDocument();

    // Inline status <select> reflects the current status.
    const statusSelect = r.getByRole('combobox') as HTMLSelectElement;
    expect(statusSelect.value).toBe('available');
  });

  it('wires the inline status select to updateWorkCenterStatus', async () => {
    renderPage();

    const codeCell = await screen.findByText('LASER-01');
    const row = codeCell.closest('tr') as HTMLElement;
    const statusSelect = within(row).getByRole('combobox');

    fireEvent.change(statusSelect, { target: { value: 'maintenance' } });

    await waitFor(() => {
      expect(mockedApi.updateWorkCenterStatus).toHaveBeenCalledWith(1, 'maintenance');
    });
  });
});
