/**
 * ToolManagement — "instrument-panel cockpit" overhaul regression.
 *
 * The standalone big-KPI card grid was removed; the four KPI counts now live in
 * the tab badges (All Tools / Checked Out / Replacement Due / Inspection Due).
 * This locks that behavior: each tab renders its label and the count badge from
 * the tool dashboard endpoint.
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import ToolManagement from './ToolManagement';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getToolDashboard: jest.fn(),
    getTools: jest.fn(),
    getToolsCheckedOut: jest.fn(),
    getToolsReplacementDue: jest.fn(),
    getToolsInspectionDue: jest.fn(),
    getToolHistory: jest.fn(),
    createTool: jest.fn(),
    checkoutTool: jest.fn(),
    checkinTool: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const dashboard = {
  total_tools: 42,
  checked_out: 7,
  replacement_due: 3,
  inspection_due: 5,
  by_status: {},
  by_type: {},
};

function renderPage() {
  return render(
    <MemoryRouter>
      <ToolManagement />
    </MemoryRouter>
  );
}

describe('ToolManagement cockpit: KPI counts live in tab badges', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getToolDashboard.mockResolvedValue(dashboard);
    mockedApi.getTools.mockResolvedValue([]);
    mockedApi.getToolsCheckedOut.mockResolvedValue([]);
    mockedApi.getToolsReplacementDue.mockResolvedValue([]);
    mockedApi.getToolsInspectionDue.mockResolvedValue([]);
    mockedApi.getToolHistory.mockResolvedValue([]);
  });

  it('renders the four tabs each carrying its dashboard count badge', async () => {
    renderPage();

    // Wait for the initial data load to resolve (dashboard drives the badges).
    const allToolsTab = await screen.findByRole('button', { name: /All Tools/ });

    const cases: { name: RegExp; count: string }[] = [
      { name: /All Tools/, count: String(dashboard.total_tools) },
      { name: /Checked Out/, count: String(dashboard.checked_out) },
      { name: /Replacement Due/, count: String(dashboard.replacement_due) },
      { name: /Inspection Due/, count: String(dashboard.inspection_due) },
    ];

    for (const c of cases) {
      const tab = screen.getByRole('button', { name: c.name });
      expect(tab).toBeInTheDocument();
      // The count badge is rendered inside the tab button.
      expect(within(tab).getByText(c.count)).toBeInTheDocument();
    }

    // Sanity: the All Tools tab matched is the same node and shows its count.
    expect(within(allToolsTab).getByText('42')).toBeInTheDocument();
  });
});
