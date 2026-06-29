/**
 * SPC cockpit-overhaul render guard.
 *
 * The SPC page was reworked into the shared instrument-panel cockpit primitives
 * (MiniStat / MiniStatStrip / CockpitPanel) and at one point shipped a
 * malformed-class bug that broke rendering. This test locks the post-overhaul
 * behavior: the MiniStat KPI strip renders with its four labels + values, and
 * selecting a characteristic mounts the control-chart and process-capability
 * panels without throwing.
 *
 * The control chart uses recharts ResponsiveContainer, which needs ResizeObserver
 * (not provided by jsdom / setupTests) — mocked at the top of this file.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import SPC from './SPC';

// recharts ResponsiveContainer needs ResizeObserver; jsdom has none.
global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as any;

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    // mount
    getSPCDashboard: jest.fn(),
    getSPCCharacteristics: jest.fn(),
    getSPCOutOfControl: jest.fn(),
    // characteristic detail
    getSPCControlLimits: jest.fn(),
    getSPCCapability: jest.fn(),
    getSPCMeasurements: jest.fn(),
    getSPCChartData: jest.fn(),
    getSPCViolations: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const characteristic = {
  id: 7,
  name: 'Bore Diameter',
  part_id: 10,
  nominal: 1.5,
  usl: 1.55,
  lsl: 1.45,
  chart_type: 'xbar_r',
};

function renderSPC() {
  return render(
    <MemoryRouter>
      <SPC />
    </MemoryRouter>
  );
}

describe('SPC cockpit overhaul', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getSPCDashboard.mockResolvedValue({
      data: {
        characteristics_monitored: 3,
        out_of_control_count: 1,
        avg_cpk: 1.42,
        measurements_today: 12,
      },
    });
    mockedApi.getSPCCharacteristics.mockResolvedValue({ data: [characteristic] });
    mockedApi.getSPCOutOfControl.mockResolvedValue({ data: [] });

    mockedApi.getSPCControlLimits.mockResolvedValue({ data: { ucl: 1.55, cl: 1.5, lcl: 1.45 } });
    mockedApi.getSPCCapability.mockResolvedValue({ data: { cp: 1.6, cpk: 1.42, pp: 1.55, ppk: 1.38 } });
    mockedApi.getSPCMeasurements.mockResolvedValue({
      data: [
        { id: 1, value: 1.5, measured_by: 'Operator A', measured_at: '2026-06-28T10:00:00Z', notes: '' },
      ],
    });
    mockedApi.getSPCChartData.mockResolvedValue({
      data: [
        { index: 1, value: 1.5, timestamp: '2026-06-28T10:00:00Z' },
        { index: 2, value: 1.51, timestamp: '2026-06-28T10:05:00Z' },
      ],
    });
    mockedApi.getSPCViolations.mockResolvedValue({ data: [] });
  });

  it('renders the MiniStat KPI strip with its labels and values', async () => {
    renderSPC();

    // Wait for the initial dashboard load to resolve.
    expect(await screen.findByText('Characteristics Monitored')).toBeInTheDocument();
    expect(screen.getByText('Out-of-Control Alerts')).toBeInTheDocument();
    expect(screen.getByText('Average Cpk')).toBeInTheDocument();
    expect(screen.getByText('Measurements Today')).toBeInTheDocument();

    // Values come from the dashboard stats.
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText('12')).toBeInTheDocument();
    expect(screen.getByText('1.42')).toBeInTheDocument(); // avg_cpk toFixed(2)

    // Page heading is present.
    expect(screen.getByRole('heading', { name: 'Statistical Process Control' })).toBeInTheDocument();
  });

  it('mounts the control-chart and process-capability panels when a characteristic is selected', async () => {
    renderSPC();

    // The characteristic selector lists the mocked characteristic.
    const charButton = await screen.findByRole('button', { name: /Bore Diameter/ });
    fireEvent.click(charButton);

    // Wait for the detail fetch to resolve and the panels to mount.
    await waitFor(() => {
      expect(mockedApi.getSPCControlLimits).toHaveBeenCalledWith(7);
    });

    // Control-chart panel mounts with the characteristic name in its title.
    expect(await screen.findByText('Control Chart: Bore Diameter')).toBeInTheDocument();

    // Process-capability panel mounts and renders the capability metrics.
    expect(screen.getByText('Process Capability')).toBeInTheDocument();
    expect(screen.getByText('Cp')).toBeInTheDocument();
    expect(screen.getByText('Cpk')).toBeInTheDocument();
    expect(screen.getByText('Pp')).toBeInTheDocument();
    expect(screen.getByText('Ppk')).toBeInTheDocument();

    // The recent-measurements panel mounts with its loaded row.
    expect(screen.getByText('Recent Measurements')).toBeInTheDocument();
    expect(screen.getByText('Operator A')).toBeInTheDocument();
  });
});
