/**
 * WorkOrderDetail — resolve-blocker feedback.
 *
 * Resolving a blocker used to succeed silently (the list simply refreshed);
 * now success shows a toast naming the blocker, mirroring the page's other
 * mutation toasts. The error path (verbatim server detail in an error toast)
 * is pinned as existing behavior.
 *
 * AND THE GREEN TOAST IS NO LONGER UNCONDITIONAL. A shop owner resolved a
 * blocker on a held nest, read "Resolved blocker", and the operation was still
 * ON_HOLD — a second blocker still named it, so the server withheld the resume
 * and the response could not say so. `operation_outcome` says so now, and two of
 * its states earn `warning` rather than `success`: the operation is still held,
 * or it resumed but landed PENDING (off the dispatch board and off the kiosk,
 * which surface READY only). The suite below pins BOTH warnings AND the two
 * cases that must stay green — a blocker that never held anything must not
 * acquire a "still held" notice, which would be a new falsehood, not a fix.
 *
 * The note capture goes through the shared
 * InputDialog (the native prompt() is gone), so the tests drive the dialog:
 * open via the page's Resolve button, then submit via the DIALOG's Resolve
 * button (scoped `within(dialog)` — the two buttons share a name). Dialog
 * mechanics themselves are covered in WorkOrderDetail.resolveBlockerDialog
 * .test.tsx; this file owns the toast feedback.
 *
 * Harness mirrors WorkOrderDetail.correctCount.test.tsx (side-channels
 * mocked), plus ToastProvider so the global toast text actually renders.
 */

import React from 'react';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import api from '../services/api';
import WorkOrderDetail from './WorkOrderDetail';
import { ToastProvider } from '../components/ui';
import { WorkOrderBlocker } from '../types/aiForward';
// Imported rather than retyped: the assertion below has to fail if the page
// ever stops printing the SHARED pending explanation and grows a copy.
import { offTheBoardSentence, stillHeldSentence } from '../components/kiosk/heldOperations';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrder: jest.fn(),
    getOperationDetails: jest.fn(),
    getMaterialRequirements: jest.fn(),
    getWorkOrderBlockers: jest.fn(),
    resolveWorkOrderBlocker: jest.fn(),
    getActiveUsers: jest.fn(),
    getUsers: jest.fn(),
    getDocuments: jest.fn(),
    // MaterialTiesPanel loads on mount; an unmocked method is `undefined` and
    // surfaces as a silent <ErrorState role="alert"> instead of a red test.
    getMaterialAllocations: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'admin', is_superuser: false },
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

const BLOCKER: WorkOrderBlocker = {
  id: 7,
  company_id: 1,
  work_order_id: 42,
  operation_id: null,
  material_part_id: null,
  category: 'material_missing',
  severity: 'high',
  status: 'open',
  title: 'Material missing',
  note: null,
  reported_at: '2026-01-01T00:00:00Z',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

const workOrderFixture = {
  id: 42,
  version: 1,
  work_order_number: 'WO-0042',
  part_id: 100,
  work_order_type: 'production',
  quantity_ordered: 10,
  quantity_complete: 4,
  quantity_scrapped: 0,
  status: 'in_progress',
  priority: 3,
  estimated_hours: 8,
  actual_hours: 2,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  operations: [],
};

function renderDetail() {
  return render(
    <ToastProvider>
      <MemoryRouter initialEntries={['/work-orders/42']}>
        <Routes>
          <Route path="/work-orders/:id" element={<WorkOrderDetail />} />
        </Routes>
      </MemoryRouter>
    </ToastProvider>
  );
}

// Opens the InputDialog via the page's Resolve button and submits it with the
// pre-filled default note ("Resolved") via the dialog's own Resolve button.
async function resolveViaDialog() {
  fireEvent.click(await screen.findByRole('button', { name: /^resolve$/i }));
  const dialog = await screen.findByRole('dialog');
  expect(within(dialog).getByLabelText(/resolution note/i)).toHaveValue('Resolved');
  // Async act so the submit's whole microtask chain (api settle -> toast ->
  // pending-cleared) flushes inside act — no unwrapped-update warnings.
  await act(async () => {
    fireEvent.click(within(dialog).getByRole('button', { name: /^resolve$/i }));
  });
  return dialog;
}

/**
 * THE TOAST'S VARIANT, not just its words.
 *
 * Without these, every assertion in this file passes on a build that fires
 * `showToast('success', ...)` with the identical long message: the shortfall
 * would still be printed, in GREEN, with `role="status"` -- which is a green
 * toast over a job that is still stopped, i.e. the exact defect this suite
 * exists to prevent, wearing longer words. `warning` renders amber and
 * interrupts the screen reader with `role="alert"`; `success` renders green and
 * waits for a pause with `role="status"` (see components/ui/Toast.tsx).
 */
function toastPanel(node: HTMLElement): HTMLElement {
  const panel = node.closest('[role="alert"], [role="status"]');
  expect(panel).not.toBeNull();
  return panel as HTMLElement;
}

/** Amber + `role="alert"`: succeeded, but did not do everything asked. */
function expectWarningToast(node: HTMLElement): void {
  const panel = toastPanel(node);
  expect(panel).toHaveAttribute('role', 'alert');
  expect(panel.className).toContain('bg-amber-600');
  expect(panel.className).not.toContain('bg-green-600');
}

/** Green + `role="status"`: it really did all of it. */
function expectSuccessToast(node: HTMLElement): void {
  const panel = toastPanel(node);
  expect(panel).toHaveAttribute('role', 'status');
  expect(panel.className).toContain('bg-green-600');
  expect(panel.className).not.toContain('bg-amber-600');
}

beforeEach(() => {
  jest.clearAllMocks();

  mockedApi.getWorkOrder.mockResolvedValue({ ...workOrderFixture });
  mockedApi.getOperationDetails.mockResolvedValue({ all_operations: [] });
  mockedApi.getMaterialRequirements.mockResolvedValue(null);
  mockedApi.getWorkOrderBlockers.mockResolvedValue([BLOCKER]);
  mockedApi.resolveWorkOrderBlocker.mockResolvedValue({ ...BLOCKER, status: 'resolved' });
  mockedApi.getActiveUsers.mockResolvedValue([]);
  mockedApi.getUsers.mockResolvedValue([]);
  mockedApi.getDocuments.mockResolvedValue([]);
  mockedApi.getMaterialAllocations.mockResolvedValue([]);
});

describe('WorkOrderDetail resolve-blocker toast', () => {
  it('shows a success toast naming the blocker after resolve + refetch', async () => {
    renderDetail();

    await resolveViaDialog();

    await waitFor(() => expect(mockedApi.resolveWorkOrderBlocker).toHaveBeenCalledWith(7, 'Resolved'));
    // Toast fires after the non-optimistic refetch completes (and the dialog closes).
    const green = await screen.findByText('Resolved blocker "Material missing"');
    expect(green).toBeInTheDocument();
    expectSuccessToast(green);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('warns instead of celebrating when the operation is STILL on hold', async () => {
    // The reported defect, exactly: the blocker closed, another one still names
    // the operation, and the server withheld the resume.
    mockedApi.resolveWorkOrderBlocker.mockResolvedValue({
      ...BLOCKER,
      status: 'resolved',
      operation_outcome: {
        operation_id: 900,
        operation_status: 'on_hold',
        operation_resumed: false,
        resume_withheld_reason: 'other_blockers_open',
        operation_still_held: true,
        open_blockers: [
          { id: 8, title: 'Fixture on order', category: 'tooling_missing', severity: 'high', status: 'open' },
        ],
      },
    });
    renderDetail();

    await resolveViaDialog();

    const toast = await screen.findByText(/STILL on hold/i);
    expect(toast).toBeInTheDocument();
    expectWarningToast(toast);
    // It still says the blocker closed — the write DID succeed.
    expect(toast.textContent).toContain('Resolved blocker "Material missing"');
    // ...and it names what is still in the way, verbatim from the server.
    expect(toast.textContent).toContain('Fixture on order');
    // ...and BOTH ways off the hold, because naming only one describes half the
    // exits: close the other blocker, or use the Clear Hold control Release 1 put
    // on the operation's own row on this page.
    expect(toast.textContent).toContain('Resolve that one too');
    expect(toast.textContent).toContain('Clear Hold');
    // Pinned to the SHARED sentence, not to a paraphrase: if the page ever grows
    // its own copy of this wording, this fails rather than drifting quietly.
    expect(toast.textContent).toContain(
      stillHeldSentence('other_blockers_open', [
        { id: 8, title: 'Fixture on order', category: 'tooling_missing', severity: 'high', status: 'open' },
      ])
    );
    // Never the plain green line.
    expect(screen.queryByText('Resolved blocker "Material missing"')).not.toBeInTheDocument();
  });

  it('counts and names EVERY blocker still in the way', async () => {
    mockedApi.resolveWorkOrderBlocker.mockResolvedValue({
      ...BLOCKER,
      status: 'resolved',
      operation_outcome: {
        operation_id: 900,
        operation_status: 'on_hold',
        operation_resumed: false,
        resume_withheld_reason: 'other_blockers_open',
        operation_still_held: true,
        open_blockers: [
          { id: 8, title: 'Fixture on order', category: 'tooling_missing', severity: 'high', status: 'open' },
          { id: 9, title: 'Awaiting MTR', category: 'material_missing', severity: 'high', status: 'acknowledged' },
        ],
      },
    });
    renderDetail();

    await resolveViaDialog();

    const toast = await screen.findByText(/STILL on hold/i);
    expectWarningToast(toast);
    expect(toast.textContent).toContain('2 other blockers are');
    expect(toast.textContent).toContain('Fixture on order; Awaiting MTR');
  });

  it('warns when the hold cleared but the job did not go back on the board', async () => {
    mockedApi.resolveWorkOrderBlocker.mockResolvedValue({
      ...BLOCKER,
      status: 'resolved',
      operation_outcome: {
        operation_id: 900,
        operation_status: 'pending',
        operation_resumed: true,
        resume_withheld_reason: null,
        operation_still_held: false,
        open_blockers: [],
      },
    });
    renderDetail();

    await resolveViaDialog();

    const toast = await screen.findByText(/did NOT go back on the board/i);
    expectWarningToast(toast);
    expect(toast.textContent).toContain('Resolved blocker "Material missing"');
    expect(toast.textContent).not.toMatch(/still on hold/i);
    // The SAME sentence the page's Clear Hold prints, from the shared
    // `offTheBoardSentence` — the shortfall is one server fact, worded once.
    expect(toast.textContent).toContain(offTheBoardSentence('The hold cleared, but the job'));
  });

  it('stays green when the operation genuinely came off hold', async () => {
    mockedApi.resolveWorkOrderBlocker.mockResolvedValue({
      ...BLOCKER,
      status: 'resolved',
      operation_outcome: {
        operation_id: 900,
        operation_status: 'ready',
        operation_resumed: true,
        resume_withheld_reason: null,
        operation_still_held: false,
        open_blockers: [],
      },
    });
    renderDetail();

    await resolveViaDialog();

    const green = await screen.findByText('Resolved blocker "Material missing"');
    expect(green).toBeInTheDocument();
    expectSuccessToast(green);
  });

  it('stays green for a blocker that never held anything', async () => {
    // `no_operation` is a WITHHELD REASON, but nothing was ever on hold. Warning
    // here would be a new kind of dishonesty rather than a fix, so the reason
    // name alone must never drive the warning.
    mockedApi.resolveWorkOrderBlocker.mockResolvedValue({
      ...BLOCKER,
      status: 'resolved',
      operation_outcome: {
        operation_id: null,
        operation_status: null,
        operation_resumed: false,
        resume_withheld_reason: 'no_operation',
        operation_still_held: false,
        open_blockers: [],
      },
    });
    renderDetail();

    await resolveViaDialog();

    const green = await screen.findByText('Resolved blocker "Material missing"');
    expect(green).toBeInTheDocument();
    expectSuccessToast(green);
  });

  it('shows the server detail in an error toast when the resolve is refused (existing behavior)', async () => {
    const refusal = 'Blocker already resolved by someone else';
    mockedApi.resolveWorkOrderBlocker.mockRejectedValue({
      response: { data: { detail: refusal } },
    });
    renderDetail();

    const dialog = await resolveViaDialog();

    expect(await screen.findByText(refusal)).toBeInTheDocument();
    // No false success.
    expect(screen.queryByText(/resolved blocker/i)).not.toBeInTheDocument();
    // Wait for the rejection's `finally` to clear the pending state (the
    // dialog's Resolve re-enables) so the update lands inside act().
    await waitFor(() => expect(within(dialog).getByRole('button', { name: /^resolve$/i })).toBeEnabled());
  });
});
