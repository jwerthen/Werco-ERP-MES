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
//
// PR 4.5 added the Backflush card to the Overview tab, which fires
// `getPartBackflushReadiness` on mount. This is the hand-written-api-mock
// liability the material-tie panel already recorded: there is no shared mock
// factory, so a new client method a rendered child calls has to be added to
// every suite that renders it, or the whole file dies on `is not a function`.
jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getPart: jest.fn(),
    getBOMByPart: jest.fn(),
    getRoutingByPart: jest.fn(),
    getPartReadiness: jest.fn(),
    updatePart: jest.fn(),
    getPartBackflushReadiness: jest.fn(),
    setPartBackflush: jest.fn(),
  },
}));

// PR 4.5 also made PartDetail call `useAuth()` (to gate the backflush toggle on
// `parts:edit`), and `useAuth` THROWS outside an AuthProvider rather than
// returning a null user — so a page test either provides the context or mocks
// the hook. Mocked, like every other page suite in this directory.
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'admin', is_superuser: false },
    isAuthenticated: true,
    isLoading: false,
  }),
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
    mockedApi.getPartBackflushReadiness.mockResolvedValue({
      part_id: 7,
      part_number: 'PN-7001',
      backflush_components: false,
      eligible: true,
      blockers: [],
      advisories: [],
    } as any);
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
