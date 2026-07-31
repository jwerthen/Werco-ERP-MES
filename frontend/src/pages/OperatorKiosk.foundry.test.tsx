/**
 * OperatorKiosk — Foundry-redesign flows NOT covered by the base suites:
 *
 *  - COMPLETE modal chained start (decision 6): after a successful complete
 *    the kiosk attempts a NON-optimistic clock-in to the next queued job; a
 *    server refusal is surfaced VERBATIM as a toast and the operator lands on
 *    the queue (never a pretend-started job).
 *  - COMPLETE modal amber steps banner: outstanding step records route to the
 *    steps view instead of completing blind.
 *  - Queue-card PDF chip: opens the doc viewer for that operation WITHOUT
 *    also firing the card's clock-in confirm (the stopPropagation pin), and
 *    wires the viewer to the session (api-client) transport.
 *  - Legacy scrap vocabulary NCR default: a dimensional reason ("Out of
 *    tolerance") leaves the OPEN NCR toggle OFF — only material-defect
 *    reasons default it ON (the conservative decision-5 heuristic; the ON
 *    cases live in OperatorKiosk.test.tsx / .scrapCodes.test.tsx).
 *  - Signed-out station reset: ANY authenticated→signed-out transition (e.g.
 *    the axios interceptor clearing a dead token without navigating) resets
 *    the station state, so the next badge lands on the queue — never inside
 *    the previous operator's half-open modal.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import OperatorKiosk from './OperatorKiosk';
import api from '../services/api';
import { useAuth } from '../context/AuthContext';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkCenterQueue: jest.fn(),
    getMyActiveJob: jest.fn(),
    getWorkCenters: jest.fn(),
    getScrapReasonCodes: jest.fn(),
    getOperationSteps: jest.fn(),
    getOperationDocuments: jest.fn(),
    fetchShopFloorDocumentBlob: jest.fn(),
    clockIn: jest.fn(),
    clockOut: jest.fn(),
    completeOperation: jest.fn(),
    reportOperationProduction: jest.fn(),
    reduceOperationProduction: jest.fn(),
    holdOperation: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: jest.fn(),
}));

// The doc viewer itself is unit-tested in KioskDocViewer.test.tsx (with the
// pdf.js boundary mocked there). Here a stub records what the PAGE mounted it
// with, so these tests stay about OperatorKiosk's wiring.
const docViewerProps = jest.fn();
jest.mock('../components/kiosk/KioskDocViewer', () => ({
  __esModule: true,
  default: (props: Record<string, unknown>) => {
    docViewerProps(props);
    return <div data-testid="mock-doc-viewer" />;
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;
const mockedUseAuth = useAuth as jest.Mock;

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
};

const ACTIVE_ITEM = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-2026-0142',
  part_number: 'PN-7731',
  part_name: 'Bracket, hinge',
  operation_number: '20',
  operation_name: 'Deburr',
  work_center_id: 7,
  status: 'in_progress',
  quantity_ordered: 50,
  quantity_complete: 5,
  priority: 5,
  due_date: null,
  steps_total: 0,
  steps_recorded: 0,
};

const NEXT_ITEM = {
  ...ACTIVE_ITEM,
  operation_id: 32,
  work_order_id: 10,
  work_order_number: 'WO-2026-0143',
  part_number: 'PN-9001',
  part_name: 'Plate, cover',
  status: 'ready',
  quantity_complete: 0,
  laser_nest: NEST,
};

const ACTIVE_JOB = {
  time_entry_id: 501,
  clock_in: new Date(Date.now() - 60_000).toISOString(),
  entry_type: 'run',
  work_order_id: 9,
  operation_id: 31,
  work_center_id: 7,
  work_order_number: 'WO-2026-0142',
  part_number: 'PN-7731',
  part_name: 'Bracket, hinge',
  operation_name: 'Deburr',
  operation_number: '20',
  quantity_ordered: 50,
  quantity_complete: 5,
};

const OPERATOR = {
  id: 3,
  first_name: 'Rosa',
  last_name: 'Vega',
  employee_id: 'EMP-4217',
  role: 'operator',
  email: 'r@x.y',
};

function authAs(user: object | null) {
  mockedUseAuth.mockReturnValue({
    user,
    isAuthenticated: !!user,
    isLoading: false,
    loginWithEmployeeId: jest.fn(),
    logout: jest.fn(),
  });
}

function kioskTree() {
  return (
    <MemoryRouter initialEntries={['/kiosk?kiosk=1&work_center_id=7&work_center_code=DEBUR1']}>
      <OperatorKiosk />
    </MemoryRouter>
  );
}

describe('OperatorKiosk — Foundry flows', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    authAs(OPERATOR);
    mockedApi.getWorkCenters.mockResolvedValue([]);
    mockedApi.getScrapReasonCodes.mockResolvedValue([]);
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [ACTIVE_ITEM, NEXT_ITEM] });
  });

  describe('complete modal — chained next-job start (decision 6)', () => {
    it('announces the chain on the CTA and clocks in to the next queue item after a successful complete', async () => {
      mockedApi.clockOut.mockResolvedValue({});
      mockedApi.completeOperation.mockResolvedValue({});
      mockedApi.clockIn.mockResolvedValue({ id: 601 });
      render(kioskTree());

      fireEvent.click(await screen.findByRole('button', { name: /^complete op$/i }));
      const cta = screen.getByTestId('kiosk-qty-confirm');
      expect(cta).toHaveTextContent(/complete op 20 · start WO-2026-0143/i);
      fireEvent.click(cta);

      await waitFor(() =>
        expect(mockedApi.clockIn).toHaveBeenCalledWith({
          work_order_id: 10,
          operation_id: 32,
          work_center_id: 7,
          entry_type: 'run',
          source: 'kiosk',
        })
      );
      // Strictly AFTER the complete landed — never an optimistic pre-start.
      expect(mockedApi.completeOperation.mock.invocationCallOrder[0]).toBeLessThan(
        mockedApi.clockIn.mock.invocationCallOrder[0]
      );
      expect(await screen.findByText('Clocked in to WO-2026-0143')).toBeInTheDocument();
    });

    it('surfaces a chained clock-in refusal VERBATIM and lands on the queue', async () => {
      mockedApi.clockOut.mockResolvedValue({});
      mockedApi.completeOperation.mockResolvedValue({});
      mockedApi.clockIn.mockRejectedValue({
        response: { data: { detail: 'Previous operations must be completed first' } },
      });
      render(kioskTree());

      fireEvent.click(await screen.findByRole('button', { name: /^complete op$/i }));
      fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

      // The complete itself succeeded; only the chain start was refused.
      expect(await screen.findByText('Previous operations must be completed first')).toBeInTheDocument();
      expect(screen.getByText('Completed WO-2026-0142')).toBeInTheDocument();
      // Landed on the queue view, not inside a stale modal.
      expect(screen.getByRole('region', { name: /work queue/i })).toBeInTheDocument();
      expect(screen.queryByTestId('kiosk-qty-confirm')).not.toBeInTheDocument();
    });

    it('routes the amber steps banner to the steps view instead of completing blind', async () => {
      const withSteps = { ...ACTIVE_ITEM, steps_total: 3, steps_recorded: 1 };
      mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [withSteps, NEXT_ITEM] });
      mockedApi.getOperationSteps.mockResolvedValue({
        operation_id: 31,
        work_order_id: 9,
        work_order_number: 'WO-2026-0142',
        operation_status: 'in_progress',
        is_serialized: false,
        serial_numbers: [],
        steps: [],
        steps_total: 3,
        steps_recorded: 1,
        completeness: {},
      });
      render(kioskTree());

      fireEvent.click(await screen.findByRole('button', { name: /^complete op$/i }));
      const banner = screen.getByTestId('kiosk-complete-steps-banner');
      expect(banner).toHaveTextContent(/2 step records still needed — tap to review/i);
      fireEvent.click(banner);

      // The steps view opened for the active op; no completion was attempted.
      expect(await screen.findByTestId('kiosk-steps-progress')).toBeInTheDocument();
      expect(mockedApi.getOperationSteps).toHaveBeenCalledWith(31);
      expect(mockedApi.clockOut).not.toHaveBeenCalled();
      expect(mockedApi.completeOperation).not.toHaveBeenCalled();
    });
  });

  describe('queue-card PDF chip (doc-viewer entry)', () => {
    it('opens the viewer for that operation on the NEST tab with the session transport', async () => {
      render(kioskTree());

      const card = await screen.findByRole('button', { name: /^work order WO-2026-0143/i });
      fireEvent.click(within(card).getByRole('button', { name: /open nest pdf for WO-2026-0143/i }));

      expect(await screen.findByTestId('mock-doc-viewer')).toBeInTheDocument();
      const props = docViewerProps.mock.calls[0][0] as {
        operationId: number;
        initialTab: string;
        transport: { fetchOperationDocuments: (id: number) => void };
      };
      expect(props.operationId).toBe(32);
      expect(props.initialTab).toBe('nest');
      // The injected transport is the SESSION client (interceptor-guarded).
      props.transport.fetchOperationDocuments(32);
      expect(mockedApi.getOperationDocuments).toHaveBeenCalledWith(32);
    });

    it('does NOT also fire the card tap (no clock-in confirm, no clockIn call) — the stopPropagation pin', async () => {
      render(kioskTree());

      const card = await screen.findByRole('button', { name: /^work order WO-2026-0143/i });
      fireEvent.click(within(card).getByRole('button', { name: /open nest pdf/i }));

      await screen.findByTestId('mock-doc-viewer');
      expect(screen.queryByText(/^clock in\?$/i)).not.toBeInTheDocument();
      expect(mockedApi.clockIn).not.toHaveBeenCalled();
    });
  });

  describe('scrap NCR default heuristic (legacy vocabulary)', () => {
    it('a dimensional reason ("Out of tolerance") leaves the NCR toggle OFF and omits open_ncr', async () => {
      mockedApi.reportOperationProduction.mockResolvedValue({});
      render(kioskTree());

      fireEvent.click(await screen.findByTestId('kiosk-active-scrap'));
      fireEvent.click(screen.getByTestId('kiosk-key-2'));
      fireEvent.click(screen.getByRole('button', { name: 'Out of tolerance' }));

      expect(screen.getByTestId('kiosk-report-ncr-toggle')).toHaveAttribute('aria-checked', 'false');
      fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

      await waitFor(() =>
        expect(mockedApi.reportOperationProduction).toHaveBeenCalledWith(31, {
          quantity_complete_delta: 0,
          quantity_scrapped_delta: 2,
          scrap_reason: 'Out of tolerance',
          source: 'kiosk',
        })
      );
    });
  });

  describe('signed-out station reset', () => {
    it('an interceptor-style sign-out mid-modal resets to the badge screen, and the next login lands on the queue', async () => {
      mockedApi.holdOperation.mockResolvedValue({});
      const { rerender } = render(kioskTree());

      // Park the station inside a non-queue view (the hold modal).
      fireEvent.click(await screen.findByRole('button', { name: /^hold$/i }));
      expect(screen.getByTestId('kiosk-hold-confirm')).toBeInTheDocument();

      // The axios interceptor clears a dead token WITHOUT navigating; all the
      // page sees is isAuthenticated flipping false.
      authAs(null);
      rerender(kioskTree());
      expect(await screen.findByText(/scan badge or enter id/i)).toBeInTheDocument();

      // Next operator signs in: queue view, not the previous operator's modal.
      authAs(OPERATOR);
      rerender(kioskTree());
      expect(await screen.findByRole('region', { name: /work queue/i })).toBeInTheDocument();
      expect(screen.queryByTestId('kiosk-hold-confirm')).not.toBeInTheDocument();
    });
  });
});
