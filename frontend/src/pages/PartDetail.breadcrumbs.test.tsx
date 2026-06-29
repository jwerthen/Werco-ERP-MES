/**
 * PartDetail breadcrumbs — Batch 7 navigation & wayfinding.
 *
 * The detail page adopted the shared <Breadcrumbs> trail ("Parts › {part#}"),
 * sourced from `getBreadcrumbParent` so the parent crumb stays in sync with the
 * top-bar title. This asserts the page renders a breadcrumb nav whose parent
 * link is labeled "Parts" and points back to the /parts list route, with the
 * current part number as the trailing (non-link) crumb.
 *
 * PartDetail reads a route :id, so it renders under a MemoryRouter at /parts/7
 * with a matching <Route>, wrapped in ToastProvider (the page uses useToast()).
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import PartDetail from './PartDetail';

// On mount PartDetail fires getPart + getBOMByPart + getRoutingByPart +
// getPartReadiness (in a Promise.all). updatePart is mocked defensively.
jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getPart: jest.fn(),
    getBOMByPart: jest.fn(),
    getRoutingByPart: jest.fn(),
    getPartReadiness: jest.fn(),
    updatePart: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const part = {
  id: 7,
  part_number: 'PN-7001',
  name: 'Titanium Bracket',
  part_type: 'manufactured',
  status: 'active',
  revision: 'C',
  is_critical: false,
  requires_inspection: false,
  standard_cost: 42.5,
  version: 1,
};

const bom = { id: 100, part_id: 7, status: 'released', items: [] };
const routing = { id: 200, part_id: 7, status: 'draft', operations: [] };
const readiness = { ready: true, blockers: [], warnings: [], checks: {} };

function renderPartDetail() {
  return render(
    <MemoryRouter initialEntries={['/parts/7']}>
      <ToastProvider>
        <Routes>
          <Route path="/parts/:id" element={<PartDetail />} />
        </Routes>
      </ToastProvider>
    </MemoryRouter>
  );
}

describe('PartDetail breadcrumbs', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getPart.mockResolvedValue(part as any);
    mockedApi.getBOMByPart.mockResolvedValue(bom as any);
    mockedApi.getRoutingByPart.mockResolvedValue(routing as any);
    mockedApi.getPartReadiness.mockResolvedValue(readiness as any);
  });

  it('renders a breadcrumb whose parent link points to the Parts list route', async () => {
    renderPartDetail();
    await screen.findByRole('heading', { name: 'PN-7001' });

    const crumb = screen.getByRole('navigation', { name: /breadcrumb/i });
    const parentLink = within(crumb).getByRole('link', { name: 'Parts' });
    // The parent crumb links back up to the list route (from getBreadcrumbParent).
    expect(parentLink).toHaveAttribute('href', '/parts');
  });

  it('shows the current part number as the trailing (non-link) crumb', async () => {
    renderPartDetail();
    await screen.findByRole('heading', { name: 'PN-7001' });

    const crumb = screen.getByRole('navigation', { name: /breadcrumb/i });
    // The current page (part number) is present in the trail but is not a link.
    expect(within(crumb).getByText('PN-7001')).toBeInTheDocument();
    expect(within(crumb).queryByRole('link', { name: 'PN-7001' })).not.toBeInTheDocument();
  });
});
