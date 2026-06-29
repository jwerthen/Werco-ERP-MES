/**
 * PartDetail cockpit overhaul — header + Quick Stats regression.
 *
 * The detail header was rebuilt into the instrument-panel cockpit: the part
 * identity (number, type, status, name, rev) renders ONCE in a single header
 * block, and the four Quick Stats (Standard Cost, BOM Status, Routing Status,
 * Inspection) render as compact MiniStat tiles, with BOM/Routing readiness
 * surfaced as a chip row. This guards that the identity isn't duplicated and
 * that the MiniStat strip renders the expected label/value pairs and readiness
 * chips.
 *
 * PartDetail reads a route :id, so it renders under a MemoryRouter at
 * /parts/7 with a matching <Route>. It is wrapped in a ToastProvider because
 * the page (and the default Overview tab) call useToast().
 */
import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import PartDetail from './PartDetail';

// On mount PartDetail fires getPart + getBOMByPart + getRoutingByPart +
// getPartReadiness (in a Promise.all). The default Overview tab calls
// api.updatePart only on save, never on mount — mocked here defensively.
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
  is_critical: true,
  requires_inspection: true,
  standard_cost: 42.5,
  customer_name: 'Acme Aero',
  drawing_number: 'DWG-9',
  version: 1,
};

const bom = {
  id: 100,
  part_id: 7,
  status: 'released',
  items: [{ id: 1 }, { id: 2 }],
};

const routing = {
  id: 200,
  part_id: 7,
  status: 'draft',
  operations: [{ id: 1 }],
};

const readiness = {
  ready: false,
  blockers: ['Routing is not released'],
  warnings: ['BOM has no costed items'],
  checks: {},
};

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

describe('PartDetail cockpit: header identity + MiniStat Quick Stats', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getPart.mockResolvedValue(part as any);
    mockedApi.getBOMByPart.mockResolvedValue(bom as any);
    mockedApi.getRoutingByPart.mockResolvedValue(routing as any);
    mockedApi.getPartReadiness.mockResolvedValue(readiness as any);
  });

  it('renders the part identity exactly once in the header', async () => {
    renderPartDetail();

    // The part identity is the single <h1>; it must not be duplicated as a
    // second heading by the cockpit header. (The breadcrumb trail repeats the
    // number as a non-heading <span>, which is expected.)
    const headings = await screen.findAllByRole('heading', { name: 'PN-7001' });
    expect(headings).toHaveLength(1);
    expect(headings[0].tagName).toBe('H1');

    // Scope the supporting identity to the header block (the h1's container).
    // The part name also appears in the Overview tab body, so an unscoped
    // query would be ambiguous — the point is that the header carries it.
    const header = within(headings[0].closest('div.min-w-0') as HTMLElement);
    expect(header.getByText('Titanium Bracket')).toBeInTheDocument();
    expect(header.getByText('Rev C')).toBeInTheDocument();
    expect(header.getByText('Customer: Acme Aero')).toBeInTheDocument();
    expect(header.getByText('Dwg: DWG-9')).toBeInTheDocument();
    expect(header.getByText('Critical')).toBeInTheDocument();
  });

  it('renders the four Quick Stats as MiniStat tiles with their values', async () => {
    renderPartDetail();
    await screen.findByRole('heading', { name: 'PN-7001' });

    // The Quick Stats strip is the grid holding the four MiniStat tiles. Anchor
    // on "BOM Status" (unique to the strip) and walk up to the grid container,
    // then scope all tile assertions to it. ("Standard Cost" also appears as a
    // field in the Overview tab body, so unscoped queries are ambiguous.)
    const bomLabel = screen.getByText('BOM Status');
    const strip = bomLabel.closest('div.grid') as HTMLElement;
    expect(strip).toBeInTheDocument();
    const stats = within(strip);

    // Each MiniStat tile pairs an uppercase label with its value.
    function tileValue(label: string): HTMLElement {
      const labelEl = stats.getByText(label);
      return within(labelEl.closest('div.card') as HTMLElement);
    }

    // Standard Cost tile — formatted as currency.
    expect(tileValue('Standard Cost').getByText('$42.50')).toBeInTheDocument();
    // BOM Status tile — released BOM.
    expect(tileValue('BOM Status').getByText('released')).toBeInTheDocument();
    // Routing Status tile — draft routing.
    expect(tileValue('Routing Status').getByText('draft')).toBeInTheDocument();
    // Inspection tile — required.
    expect(tileValue('Inspection').getByText('Required')).toBeInTheDocument();
  });

  it('surfaces readiness blockers and warnings as chips', async () => {
    renderPartDetail();
    await screen.findByRole('heading', { name: 'PN-7001' });

    expect(screen.getByText('Routing is not released')).toBeInTheDocument();
    expect(screen.getByText('BOM has no costed items')).toBeInTheDocument();
  });
});
