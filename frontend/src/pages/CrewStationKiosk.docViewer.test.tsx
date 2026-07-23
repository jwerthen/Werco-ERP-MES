/**
 * CrewStationKiosk — the badge-gated drawing/nest viewer (Kiosk Foundry
 * redesign, crew scope).
 *
 * The doc reads live inside the shop-floor fence and need an OPERATOR (badge)
 * token — the station token is honored only by the roster queue read and the
 * badge mint — so the VIEW NEST / DRAWING entry is badge-gated exactly like
 * steps (the stepsSign pattern): no document read happens before a badge
 * establishes the identity, then the viewer mounts with a transport bound to
 * the minted operator token (kioskStationClient — fetch-based, NEVER
 * navigates). A 401 from that transport renders the crew rescan message
 * INLINE in the viewer.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import CrewStationKiosk from './CrewStationKiosk';
import * as kioskClient from '../services/kioskStationClient';
import { KioskApiError } from '../services/kioskStationClient';

// pdf.js stays out of jest: the mocked loader rejects, forcing the viewer's
// <object> fallback (same boundary mock as KioskDocViewer.test.tsx).
jest.mock('pdfjs-dist', () => ({
  GlobalWorkerOptions: {},
  getDocument: jest.fn(() => ({ promise: Promise.reject(new Error('no pdf.js in jest')) })),
}));
jest.mock('pdfjs-dist/build/pdf.worker.min.mjs?url', () => ({ __esModule: true, default: 'mock-worker-url' }), {
  virtual: true,
});

// Keep KioskApiError REAL (instanceof checks in the page) but mock every call.
jest.mock('../services/kioskStationClient', () => {
  const actual = jest.requireActual('../services/kioskStationClient');
  return {
    __esModule: true,
    ...actual,
    getStationToken: jest.fn(),
    setStationToken: jest.fn(),
    clearStationToken: jest.fn(),
    getStoredStation: jest.fn(),
    stationLogin: jest.fn(),
    getQueue: jest.fn(),
    mintBadgeToken: jest.fn(),
    getMyActiveJob: jest.fn(),
    clockIn: jest.fn(),
    clockOut: jest.fn(),
    reportProduction: jest.fn(),
    completeOperation: jest.fn(),
    holdOperation: jest.fn(),
    getOperationSteps: jest.fn(),
    getOperationDocuments: jest.fn(),
    fetchDocumentBlob: jest.fn(),
  };
});

const mocked = kioskClient as jest.Mocked<typeof kioskClient>;

const STATION = {
  id: 3,
  label: 'Laser Bay Kiosk',
  work_center_id: 7,
  work_center_code: 'LASER1',
  work_center_name: 'Laser Bay 1',
};

const NEST = {
  id: 7,
  nest_name: 'CNC0042',
  cnc_number: 'CNC-0042',
  planned_runs: 5,
  completed_runs: 2,
  remaining_runs: 3,
  material: 'A36',
  thickness: '0.25in',
  has_document: true,
  document_id: 22,
  document_file_name: 'nest.pdf',
};

const ITEM = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-2026-0142',
  part_number: 'PN-7731',
  part_name: 'Nest, sheet',
  operation_number: 'Nest 1',
  operation_name: 'Laser Cut - CNC0042',
  work_center_id: 7,
  status: 'ready',
  quantity_ordered: 50,
  quantity_complete: 0,
  quantity_scrapped: 0,
  priority: 5,
  due_date: null,
  roster: [],
  steps_total: 0,
  steps_recorded: 0,
  laser_nest: NEST,
};

const QUEUE_RES = {
  queue: [ITEM],
  server_time: new Date().toISOString(),
  station: STATION,
};

const ALICE = { id: 13, full_name: 'Alice W', employee_id: 'E013' };
const ALICE_MINT = { access_token: 'op-token-alice', user: ALICE };

const NEST_ONLY_DOCS = {
  part: { id: 4, part_number: 'PN-7731', name: 'Nest, sheet', revision: null },
  drawing: null,
  nest: { laser_nest_id: 7, nest_name: 'CNC0042', cnc_number: 'CNC-0042', document_id: 22, file_name: 'nest.pdf' },
  material: 'A36',
  critical_dims: [],
};

function renderKiosk() {
  return render(
    <MemoryRouter initialEntries={['/kiosk?kiosk=1&station=3']}>
      <CrewStationKiosk />
    </MemoryRouter>
  );
}

function unlockedStation() {
  mocked.getStationToken.mockReturnValue('station-token');
  mocked.getStoredStation.mockReturnValue(STATION);
}

/** Type a badge on the window (wedge scanner) and hit Enter. */
function scanBadge(id: string) {
  id.split('').forEach((key) => fireEvent.keyDown(window, { key }));
  fireEvent.keyDown(window, { key: 'Enter' });
}

async function openJobDetail() {
  fireEvent.click(await screen.findByRole('button', { name: /WO-2026-0142/i }));
  await screen.findByRole('region', { name: /job detail/i });
}

beforeEach(() => {
  jest.clearAllMocks();
  mocked.getStationToken.mockReturnValue(null);
  mocked.getStoredStation.mockReturnValue(null);
  mocked.getQueue.mockResolvedValue(QUEUE_RES as never);
  mocked.getMyActiveJob.mockResolvedValue({ active_jobs: [] } as never);
  mocked.mintBadgeToken.mockResolvedValue(ALICE_MINT as never);
  mocked.getOperationDocuments.mockResolvedValue(NEST_ONLY_DOCS as never);
  mocked.fetchDocumentBlob.mockResolvedValue('blob:crew-22');
});

describe('CrewStationKiosk doc viewer (badge-gated)', () => {
  it('gates VIEW NEST behind a badge scan, then mounts the viewer on the station-client transport', async () => {
    unlockedStation();
    renderKiosk();

    await openJobDetail();
    fireEvent.click(screen.getByTestId('crew-view-nest'));

    // Badge gate first — controlled documents need a recorded identity, and
    // NO document read happens before the badge establishes it.
    expect(await screen.findByText(/scan badge to view documents/i)).toBeInTheDocument();
    expect(mocked.getOperationDocuments).not.toHaveBeenCalled();

    scanBadge('E013');
    expect(await screen.findByTestId('kiosk-doc-viewer')).toBeInTheDocument();
    expect(mocked.mintBadgeToken).toHaveBeenCalledWith('E013');
    // Discovery and bytes both ride the badge-minted OPERATOR token.
    expect(mocked.getOperationDocuments).toHaveBeenCalledWith('op-token-alice', 31);
    await waitFor(() => expect(mocked.fetchDocumentBlob).toHaveBeenCalledWith('op-token-alice', 22));
    // Nest-only discovery → the NEST tab, with the blob rendered inline.
    expect(screen.getByTestId('kiosk-viewer-tab-nest')).toHaveAttribute('aria-selected', 'true');
    await waitFor(() => expect(screen.getByLabelText('Document PDF')).toHaveAttribute('data', 'blob:crew-22'));
  });

  it('hides the VIEW NEST entry when the nest has no attached PDF', async () => {
    unlockedStation();
    mocked.getQueue.mockResolvedValue({
      ...QUEUE_RES,
      queue: [{ ...ITEM, laser_nest: { ...NEST, has_document: false, document_id: null } }],
    } as never);
    renderKiosk();

    await openJobDetail();
    expect(screen.queryByTestId('crew-view-nest')).not.toBeInTheDocument();
  });

  it('a 401 from the doc transport shows the rescan message INLINE — the terminal never navigates', async () => {
    unlockedStation();
    mocked.getOperationDocuments.mockRejectedValue(new KioskApiError(401, 'Token expired', 'Token expired'));
    renderKiosk();

    await openJobDetail();
    fireEvent.click(screen.getByTestId('crew-view-nest'));
    await screen.findByText(/scan badge to view documents/i);
    scanBadge('E013');

    // The viewer mounted and reports the expiry inline, in crew language.
    expect(await screen.findByText('Badge session expired — rescan to view')).toBeInTheDocument();
    expect(screen.getByTestId('kiosk-doc-viewer')).toBeInTheDocument();
  });

  it('a bad badge scan surfaces the mint refusal on the badge panel and never opens the viewer', async () => {
    unlockedStation();
    mocked.mintBadgeToken.mockRejectedValue(new KioskApiError(401, 'Unknown badge', 'Unknown badge'));
    renderKiosk();

    await openJobDetail();
    fireEvent.click(screen.getByTestId('crew-view-nest'));
    await screen.findByText(/scan badge to view documents/i);
    scanBadge('E999');

    expect(await screen.findByText('Unknown badge')).toBeInTheDocument();
    expect(screen.queryByTestId('kiosk-doc-viewer')).not.toBeInTheDocument();
    expect(mocked.getOperationDocuments).not.toHaveBeenCalled();
  });
});
