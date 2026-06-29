/**
 * ToolManagement — Batch 5 responsive-table migration coverage.
 *
 * The tools list was migrated onto the shared DataTable + mobileCards. This locks
 * the responsive contract:
 *
 *   1. the page renders via DataTable (data-testid="data-table"),
 *   2. a sortable header reorders the desktop rows,
 *   3. the CSV export control is present, and
 *   4. the md:hidden mobile-card layout renders the same row content.
 *
 * DataTable.mobileCards renders BOTH the desktop <table> and the mobile cards into
 * jsdom (CSS-hidden only), so each tool number appears twice. Row/sort lookups are
 * scoped to the desktop <table>; the mobile assertion is scoped to the md:hidden
 * wrapper. The page early-returns a loading skeleton, so each test first awaits a
 * tool row.
 */

import React from 'react';
import { render, screen, within, fireEvent } from '@testing-library/react';
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

const baseTool = {
  description: '',
  location: 'Cage A',
  current_uses: 0,
  current_life_hours: 0,
  is_active: true,
  created_at: '2026-01-01T00:00:00Z',
};

// Deliberately NOT in tool_number order so a header sort visibly reorders rows.
const tools = [
  { ...baseTool, id: 1, tool_number: 'TL-300', name: 'End Mill', tool_type: 'cutting_tool', status: 'available' },
  { ...baseTool, id: 2, tool_number: 'TL-100', name: 'Bore Gauge', tool_type: 'gauge', status: 'checked_out' },
  { ...baseTool, id: 3, tool_number: 'TL-200', name: 'Weld Fixture', tool_type: 'fixture', status: 'available' },
];

const TOOL_NUMBERS = ['TL-100', 'TL-200', 'TL-300'];

const dashboard = {
  total_tools: 3,
  checked_out: 1,
  replacement_due: 0,
  inspection_due: 0,
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

// First desktop body cell holds the tool number (col key 'tool_number').
function getDesktopToolNumbers(): string[] {
  const table = screen.getByTestId('data-table');
  const bodyRows = within(table).getAllByRole('row').slice(1); // drop header
  return bodyRows.map((r) => within(r).getAllByRole('cell')[0].textContent?.trim() || '');
}

describe('ToolManagement — responsive DataTable (Batch 5)', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getToolDashboard.mockResolvedValue(dashboard);
    mockedApi.getTools.mockResolvedValue(tools as any);
    mockedApi.getToolsCheckedOut.mockResolvedValue([]);
    mockedApi.getToolsReplacementDue.mockResolvedValue([]);
    mockedApi.getToolsInspectionDue.mockResolvedValue([]);
    mockedApi.getToolHistory.mockResolvedValue([]);
  });

  it('renders the tools list through DataTable', async () => {
    renderPage();
    await screen.findAllByText('TL-300');

    const table = screen.getByTestId('data-table');
    // Header + 3 tool rows.
    expect(within(table).getAllByRole('row')).toHaveLength(4);
    TOOL_NUMBERS.forEach((n) => expect(within(table).getByText(n)).toBeInTheDocument());
  });

  it('reorders rows when the sortable Tool # header is clicked', async () => {
    renderPage();
    await screen.findAllByText('TL-300');

    const table = screen.getByTestId('data-table');
    const headerBtn = within(table).getByRole('button', { name: /Tool #/i });

    // Tool # is the default-sort column (defaultSort tool_number asc), so the rows
    // start ascending and the toggle cycle runs asc → desc → none from there.
    expect(getDesktopToolNumbers()).toEqual([...TOOL_NUMBERS]);
    expect(headerBtn.closest('th')).toHaveAttribute('aria-sort', 'ascending');

    // First click on the already-ascending column → descending.
    fireEvent.click(headerBtn);
    expect(getDesktopToolNumbers()).toEqual([...TOOL_NUMBERS].reverse());
    expect(headerBtn.closest('th')).toHaveAttribute('aria-sort', 'descending');

    // Second click → sort cleared, rows fall back to source order.
    fireEvent.click(headerBtn);
    expect(getDesktopToolNumbers()).toEqual(['TL-300', 'TL-100', 'TL-200']);
    expect(headerBtn.closest('th')).toHaveAttribute('aria-sort', 'none');
  });

  it('exposes the CSV export control', async () => {
    renderPage();
    await screen.findAllByText('TL-300');

    expect(screen.getByRole('button', { name: /export csv/i })).toBeInTheDocument();
  });

  it('renders the md:hidden mobile-card layout with the same row content', async () => {
    const { container } = renderPage();
    await screen.findAllByText('TL-300');

    const mobileWrapper = container.querySelector('.md\\:hidden');
    expect(mobileWrapper).not.toBeNull();
    const mobile = within(mobileWrapper as HTMLElement);

    // Each tool number renders as a mobile card title.
    TOOL_NUMBERS.forEach((n) => expect(mobile.getByText(n)).toBeInTheDocument());
    // The mobile layout is cards, not a table.
    expect(mobileWrapper!.querySelector('table')).toBeNull();
  });
});
