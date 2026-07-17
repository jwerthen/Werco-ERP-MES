/**
 * Standalone laser-nest import from the Work Orders list, plus part-less
 * (part_id NULL) laser WO row rendering.
 *
 * The list grows an "Import Nest Package" action (role-gated like the backend
 * nest endpoints: routings:create → admin/manager/supervisor) that opens the
 * LaserNestImportWizard in STANDALONE mode — no parent WO. A successful import
 * creates a fresh released laser-cutting WO and the page navigates to it.
 *
 * Standalone nest WOs carry NO part (part_id NULL, no part_number/name); the
 * list must label them ("Nest package") instead of rendering blank cells.
 *
 * The desktop table (`hidden lg:block`) and the mobile list (`lg:hidden`) BOTH
 * mount in jsdom, so shared strings can appear twice — assertions scope to the
 * desktop <table> where that matters.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import WorkOrders from './WorkOrders';
import { LaserNestPackagePreview } from '../types';

const mockNavigate = jest.fn();

jest.mock('react-router-dom', () => ({
  ...jest.requireActual('react-router-dom'),
  useNavigate: () => mockNavigate,
}));

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrders: jest.fn(),
    deleteWorkOrder: jest.fn(),
    releaseWorkOrder: jest.fn(),
    previewLaserNestPackageStandalone: jest.fn(),
    importLaserNestPackageStandalone: jest.fn(),
  },
}));

// Mutable so individual tests can drop to a non-managing role.
let mockUser: { id: number; role: string; is_superuser: boolean } = {
  id: 1,
  role: 'admin',
  is_superuser: true,
};

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: mockUser,
    isAuthenticated: true,
    isLoading: false,
  }),
}));

jest.mock('../hooks/useWebSocket', () => ({
  useWebSocket: jest.fn(),
}));

jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

const productionWorkOrder = {
  id: 1,
  work_order_number: 'WO-1001',
  part_id: 10,
  work_order_type: 'production',
  part_number: 'PN-AAA',
  part_name: 'Bracket Assembly',
  part_type: 'manufactured',
  status: 'in_progress' as const,
  priority: 3,
  quantity_ordered: 50,
  quantity_complete: 10,
  customer_name: 'Acme Aero',
};

/** A standalone laser nest WO: no parent, no part — quantity is sheet runs. */
const standaloneLaserWorkOrder = {
  id: 2,
  work_order_number: 'WO-1002',
  part_id: null,
  work_order_type: 'laser_cutting',
  part_number: null,
  part_name: null,
  part_type: null,
  status: 'released' as const,
  priority: 3,
  quantity_ordered: 12,
  quantity_complete: 0,
};

const preview: LaserNestPackagePreview = {
  package_name: 'nests.zip',
  nest_count: 1,
  total_planned_runs: 5,
  nests: [
    {
      source_file: 'sheet-1.pdf',
      nest_name: 'Sheet 1',
      cnc_number: '8001',
      cnc_file_name: null,
      planned_runs: 5,
      material: '304 SS',
      thickness: '0.125"',
      sheet_size: '48x96',
      confidence: 'high',
    },
  ],
};

function renderWorkOrders() {
  return render(
    <MemoryRouter>
      <WorkOrders />
    </MemoryRouter>
  );
}

async function getDesktopTable(): Promise<HTMLElement> {
  const woLinks = await screen.findAllByRole('link', { name: 'WO-1001' });
  const tableLink = woLinks.find((el) => el.closest('table'));
  const table = tableLink?.closest('table');
  if (!table) throw new Error('expected a WO link inside the desktop <table>');
  return table as HTMLElement;
}

describe('WorkOrders — part-less standalone laser WO row', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockUser = { id: 1, role: 'admin', is_superuser: true };
    mockedApi.getWorkOrders.mockResolvedValue([productionWorkOrder, standaloneLaserWorkOrder]);
  });

  it('renders the laser WO with a "Nest package" label instead of blank part cells', async () => {
    renderWorkOrders();
    const table = await getDesktopTable();

    // The part-less laser row renders, links to its detail page, and is labeled.
    expect(within(table).getByRole('link', { name: 'WO-1002' })).toHaveAttribute('href', '/work-orders/2');
    expect(within(table).getByText('Nest package')).toBeInTheDocument();
    expect(within(table).getByText('Laser sheet runs')).toBeInTheDocument();

    // The parted row is unaffected.
    expect(within(table).getByText('PN-AAA')).toBeInTheDocument();
    // Nothing leaks a literal "undefined"/"null" into the DOM.
    expect(screen.queryByText(/undefined|null/)).not.toBeInTheDocument();
  });
});

describe('WorkOrders — standalone Import Nest Package action', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockUser = { id: 1, role: 'admin', is_superuser: true };
    mockedApi.getWorkOrders.mockResolvedValue([productionWorkOrder]);
    mockedApi.previewLaserNestPackageStandalone.mockResolvedValue(preview);
    mockedApi.importLaserNestPackageStandalone.mockResolvedValue({
      package: preview,
      child_work_order: { id: 777, work_order_number: 'WO-0777' },
    });
  });

  it('runs the wizard end-to-end against the standalone endpoints and navigates to the created WO', async () => {
    renderWorkOrders();
    await getDesktopTable();

    fireEvent.click(screen.getByRole('button', { name: /import nest package/i }));

    // Wizard opens in standalone mode (pick step).
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/creates a new released laser cutting work order/i)).toBeInTheDocument();

    const zip = new File(['PK'], 'nests.zip', { type: 'application/zip' });
    fireEvent.change(within(dialog).getByLabelText(/zip package/i), { target: { files: [zip] } });
    fireEvent.click(within(dialog).getByRole('button', { name: /^preview$/i }));

    await waitFor(() => expect(mockedApi.previewLaserNestPackageStandalone).toHaveBeenCalledTimes(1));
    fireEvent.click(await within(dialog).findByRole('button', { name: /^import 1 nest$/i }));

    await waitFor(() => expect(mockedApi.importLaserNestPackageStandalone).toHaveBeenCalledTimes(1));
    const [payload] = mockedApi.importLaserNestPackageStandalone.mock.calls[0];
    expect(payload.rows).toEqual([
      expect.objectContaining({ source_file: 'sheet-1.pdf', cnc_number: '8001', planned_runs: 5 }),
    ]);

    // Success routes to the freshly created laser WO's detail page.
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith('/work-orders/777'));
  });

  it('hides the action from roles without routings:create (operator)', async () => {
    mockUser = { id: 2, role: 'operator', is_superuser: false };
    renderWorkOrders();
    await getDesktopTable();

    expect(screen.queryByRole('button', { name: /import nest package/i })).not.toBeInTheDocument();
    // The New Work Order entry point is untouched.
    expect(screen.getAllByRole('link', { name: /new work order/i }).length).toBeGreaterThan(0);
  });
});
