/**
 * Quality "instrument-panel cockpit" overhaul — render + interaction regression.
 *
 * The overhaul added a 3-up MiniStat strip (Open NCRs / Open CARs / Pending FAIs)
 * above the NCR/CAR/FAI tabs. Each tile is a <button> that switches the active
 * tab; the NCR tile additionally applies the `open` status filter. This locks:
 *   - the three tiles render with their labels + summary values, and
 *   - clicking a tile switches to its tab (and, for NCRs, marks itself active /
 *     applies the open filter).
 *
 * The summary/list endpoints the page calls on mount are all mocked; a missing
 * mock would make loadData reject and the page would never leave its spinner.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Quality from './Quality';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getNCRs: jest.fn(),
    getCARs: jest.fn(),
    getFAIs: jest.fn(),
    getQualitySummary: jest.fn(),
    getParts: jest.fn(),
    createNCR: jest.fn(),
    createCAR: jest.fn(),
    createFAI: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const openNcr = {
  id: 1,
  ncr_number: 'NCR-0001',
  quantity_affected: 2,
  source: 'in_process',
  status: 'open',
  disposition: 'pending',
  title: 'Surface scratch',
  description: 'Scratch on face',
  created_at: '2026-06-20T12:00:00Z',
};

const closedNcr = {
  id: 2,
  ncr_number: 'NCR-0002',
  quantity_affected: 1,
  source: 'final_inspection',
  status: 'closed',
  disposition: 'use_as_is',
  title: 'Minor burr',
  description: 'Burr removed',
  created_at: '2026-06-21T12:00:00Z',
};

const summary = { open_ncrs: 3, open_cars: 2, pending_fais: 1 };

function renderQuality() {
  return render(
    <MemoryRouter initialEntries={['/quality']}>
      <Quality />
    </MemoryRouter>
  );
}

describe('Quality cockpit MiniStat strip', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getNCRs.mockResolvedValue([openNcr, closedNcr]);
    mockedApi.getCARs.mockResolvedValue([]);
    mockedApi.getFAIs.mockResolvedValue([]);
    mockedApi.getQualitySummary.mockResolvedValue(summary);
    mockedApi.getParts.mockResolvedValue([]);
  });

  it('renders the 3-up MiniStat strip with summary values', async () => {
    renderQuality();

    // The strip renders three clickable tiles; wait for the load to finish.
    const ncrTile = await screen.findByRole('button', { name: /Open NCRs/i });
    const carTile = screen.getByRole('button', { name: /Open CARs/i });
    const faiTile = screen.getByRole('button', { name: /Pending FAIs/i });

    expect(within(ncrTile).getByText('3')).toBeInTheDocument();
    expect(within(carTile).getByText('2')).toBeInTheDocument();
    expect(within(faiTile).getByText('1')).toBeInTheDocument();

    // Default tab is NCR.
    expect(screen.getByText('Non-Conformance Reports')).toBeInTheDocument();
  });

  it('clicking the Open CARs tile switches to the CAR tab', async () => {
    renderQuality();

    const carTile = await screen.findByRole('button', { name: /Open CARs/i });
    // Sanity: we start on the NCR tab.
    expect(screen.getByText('Non-Conformance Reports')).toBeInTheDocument();

    fireEvent.click(carTile);

    expect(await screen.findByText('Corrective Action Requests')).toBeInTheDocument();
    expect(screen.queryByText('Non-Conformance Reports')).not.toBeInTheDocument();
    // The CAR tile reflects the active segment.
    expect(carTile).toHaveAttribute('aria-pressed', 'true');
  });

  it('clicking the Pending FAIs tile switches to the FAI tab', async () => {
    renderQuality();

    const faiTile = await screen.findByRole('button', { name: /Pending FAIs/i });
    fireEvent.click(faiTile);

    expect(await screen.findByText('First Article Inspections')).toBeInTheDocument();
    expect(faiTile).toHaveAttribute('aria-pressed', 'true');
  });

  it('clicking the Open NCRs tile applies the open filter (active + closed row hidden)', async () => {
    renderQuality();

    const ncrTile = await screen.findByRole('button', { name: /Open NCRs/i });
    // No filter applied yet: both NCR rows are visible.
    expect(screen.getByText('NCR-0001')).toBeInTheDocument();
    expect(screen.getByText('NCR-0002')).toBeInTheDocument();
    // Tile starts inactive (default filter is empty on /quality).
    expect(ncrTile).toHaveAttribute('aria-pressed', 'false');

    fireEvent.click(ncrTile);

    await waitFor(() => {
      expect(ncrTile).toHaveAttribute('aria-pressed', 'true');
    });
    // Filter switched to "open": the open NCR stays, the closed one is filtered out.
    expect(screen.getByText('NCR-0001')).toBeInTheDocument();
    expect(screen.queryByText('NCR-0002')).not.toBeInTheDocument();
  });
});
