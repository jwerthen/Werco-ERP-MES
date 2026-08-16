/**
 * Receiving — "Clear Hold" (clear the inspection hold).
 *
 * The non-destructive way off the inspection queue, added because Void was the
 * ONLY exit and it un-receives the material entirely, forcing a re-key. This
 * verb keeps the receipt and its lot/heat/cert exactly as keyed, posts the
 * material into stock, and drops the row off the queue.
 *
 * Covered here (frontend contract only — the server-side effects are pinned by
 * backend/tests/api/test_receipt_clear_inspection.py):
 *  - RBAC parity with POST /receiving/receipt/{id}/clear-inspection, which is
 *    [admin, manager, supervisor, quality] — byte-for-byte the role list of
 *    /receiving/inspect/{id}: the control renders for all four and is ABSENT for
 *    an operator. SUPERVISOR is in the list by owner decision, overruling an
 *    earlier draft that excluded it: withholding the waiver from a tier that
 *    holds Inspect does not keep a mis-ticked receipt on the queue, it pushes the
 *    receipt through Inspect → Visual → Pass, stamping a named inspector and a
 *    timestamp onto an inspection that never happened. So the two lists move in
 *    LOCKSTEP, and this suite is where that is asserted from the UI side;
 *  - the control is QUEUE-ONLY: it is not on history rows and not in the per-PO
 *    receipt sub-table, both of which show receipts the endpoint would 409;
 *  - a blank / whitespace-only reason never reaches the API (InputDialog
 *    disables submit on an empty trimmed value);
 *  - a confirmed submit calls api.clearReceiptInspection with the receipt id and
 *    the TRIMMED reason, toasts success, and re-pulls the queue;
 *  - a server refusal (409 — also the replay guard) surfaces the verbatim
 *    `detail` as an error toast, keeps the dialog open, and leaves the row;
 *  - the action is NON-OPTIMISTIC (house rule for server-gated actions): nothing
 *    moves until the server has answered — the row is still on the queue and no
 *    success toast has fired while the call is in flight;
 *  - the row button is labelled with the VERB ("Clear Hold"), not the status words
 *    "Not Required" — which this same page already renders as a StatusBadge on
 *    History rows, so the two would collide;
 *  - the old "Mis-ticked? Manager/Quality clears it" hint is GONE, and stays gone:
 *    it rendered only for a user who could Inspect but NOT clear, and widening the
 *    gate emptied that set — the sentence is now false for every role that reaches
 *    this queue. Every RBAC case below asserts it renders for nobody, so a
 *    re-narrowed gate can't quietly bring back a hint pointing at the wrong roles;
 *  - clearing an AGED hold widens the History window, because History is a 30-day
 *    window on received_at while the inspection queue never ages out: without this
 *    the receipt would vanish from both surfaces.
 *
 * The api service + AuthContext are mocked at the module boundary; the real
 * ToastProvider wraps the page so toast text is assertable (sibling-test pattern
 * from Receiving.correctVoid.test.tsx).
 */
import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ReceivingPage from './Receiving';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import type { ClearedReceipt } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getOpenPOsForReceiving: jest.fn(),
    getReceivingLocations: jest.fn(),
    getReceivingStats: jest.fn(),
    getInspectionQueue: jest.fn(),
    getReceivingHistory: jest.fn(),
    getPOForReceiving: jest.fn(),
    getReceiptDetail: jest.fn(),
    clearReceiptInspection: jest.fn(),
  },
}));

let mockAuthUser: { id: number; role: string } = { id: 1, role: 'manager' };
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockAuthUser, isAuthenticated: true, isLoading: false }),
}));

const mockApi = api as jest.Mocked<typeof api>;

const http = (status: number, detail?: string) => {
  const err = new Error(detail || 'error') as Error & {
    response: { status: number; data: { detail?: string } };
  };
  err.response = { status, data: { detail } };
  return err;
};

const RECEIPT_ID = 42;
const RECEIPT_NUMBER = 'RCV-20260618-001';

const QUEUE_ITEM = {
  receipt_id: RECEIPT_ID,
  receipt_number: RECEIPT_NUMBER,
  po_number: 'PO-1001',
  po_id: 11,
  vendor_name: 'Acme Metals',
  part_id: 7,
  part_number: 'PN-555',
  part_name: 'Bracket',
  quantity_received: 5,
  lot_number: 'LOT-9',
  coc_attached: true,
  received_at: '2026-06-18T12:00:00Z',
  days_pending: 1,
};

const HISTORY_ITEM = {
  receipt_id: 77,
  receipt_number: 'RCV-20260618-002',
  po_number: 'PO-1002',
  part_number: 'PN-777',
  quantity_received: 5,
  quantity_accepted: 5,
  quantity_rejected: 0,
  lot_number: 'LOT-77',
  inspection_status: 'accepted',
  status: 'accepted',
  received_at: '2026-06-18T12:00:00Z',
  received_by_name: 'Riley Dockhand',
};

// A PO whose line already carries a receipt, so the Receive tab's collapsible
// "Receipt History" sub-table renders (it is gated on lines[].receipts.length).
const OPEN_PO = {
  po_id: 5,
  po_number: 'PO-2001',
  vendor_id: 1,
  vendor_name: 'Acme Metals',
  vendor_code: 'VND-001',
  order_date: null,
  required_date: null,
  expected_date: null,
  status: 'sent',
  lines: [
    {
      line_id: 51,
      line_number: 1,
      part_id: 7,
      part_number: 'PN-555',
      part_name: 'Bracket',
      quantity_ordered: 10,
      quantity_received: 5,
      quantity_remaining: 5,
      unit_price: 3.5,
      required_date: null,
      requires_inspection: false,
      is_closed: false,
      receipts: [
        {
          receipt_id: 91,
          receipt_number: 'RCV-20260618-091',
          quantity_received: 5,
          lot_number: 'LOT-91',
          status: 'pending_inspection',
          received_at: '2026-06-18T12:00:00Z',
        },
      ],
    },
  ],
  total_lines: 1,
};

// What the endpoint returns on success: ACCEPTED / NOT_REQUIRED (never PASSED —
// no inspection happened) with requires_inspection flipped false, which is the
// only record that stock was placed.
const CLEARED: ClearedReceipt = {
  id: RECEIPT_ID,
  receipt_number: RECEIPT_NUMBER,
  status: 'accepted',
  inspection_status: 'not_required',
  requires_inspection: false,
};

// The hint the gate widening deleted. Kept as a named constant precisely BECAUSE
// nothing renders it any more: every RBAC case asserts its absence, so it is the
// tripwire for someone re-adding a "go ask a manager" pointer that no longer has a
// true role to point at.
const REMOVED_HINT = /Manager\/Quality clears it/i;
const CLEAR_LABEL = new RegExp(`Clear the inspection hold on receipt ${RECEIPT_NUMBER}`, 'i');
const ANY_CLEAR_LABEL = /clear the inspection hold on receipt/i;
const DIALOG_TITLE = /Clear inspection hold/i;
const REASON_FIELD = /Reason this material did not need inspection/i;
const SUBMIT_LABEL = /Clear Hold & Post to Stock/i;

const renderTab = (tab: 'receive' | 'queue' | 'history') =>
  render(
    <MemoryRouter initialEntries={[`/receiving?tab=${tab}`]}>
      <ToastProvider>
        <ReceivingPage />
      </ToastProvider>
    </MemoryRouter>,
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockAuthUser = { id: 1, role: 'manager' };
  mockApi.getOpenPOsForReceiving.mockResolvedValue([OPEN_PO]);
  mockApi.getReceivingLocations.mockResolvedValue([]);
  mockApi.getReceivingStats.mockResolvedValue({
    pending_inspection: 1,
    receipts_in_period: 1,
    acceptance_rate: 100,
    rejections_in_period: 0,
  });
  mockApi.getInspectionQueue.mockResolvedValue([QUEUE_ITEM]);
  mockApi.getReceivingHistory.mockResolvedValue([HISTORY_ITEM]);
  mockApi.getPOForReceiving.mockResolvedValue(OPEN_PO);
});

// The queue renders a desktop table AND a parallel mobile-card list, so every
// row control's aria-label appears twice; the [0] instance opens the same dialog.
const clearControls = () => screen.queryAllByRole('button', { name: ANY_CLEAR_LABEL });

const openClearDialog = async () => {
  await waitFor(() => expect(screen.getAllByRole('button', { name: CLEAR_LABEL }).length).toBeGreaterThan(0));
  fireEvent.click(screen.getAllByRole('button', { name: CLEAR_LABEL })[0]);
  return screen.findByRole('heading', { name: DIALOG_TITLE });
};

const typeReason = (value: string) => {
  fireEvent.change(screen.getByLabelText(REASON_FIELD), { target: { value } });
};

const submit = () => fireEvent.click(screen.getByRole('button', { name: SUBMIT_LABEL }));

describe('Receiving — "Not Required" RBAC gating', () => {
  // Mirrors require_role([ADMIN, MANAGER, SUPERVISOR, QUALITY]) on the endpoint —
  // the same four roles as /receiving/inspect/{id}, which is the point of the list.
  // platform_admin rides along because api/deps.py :: require_role short-circuits on
  // it before the list is read, AND because utils/permissions.ts grants it
  // `receiving:inspect`: left off this control it would be the one role on the queue
  // holding Inspect and not Clear Hold, i.e. the fabricated-pass path this whole
  // widening exists to close, with the "ask a manager" hint already deleted.
  it.each(['admin', 'manager', 'supervisor', 'quality', 'platform_admin'])(
    'renders the control on a queue row for %s',
    async role => {
      mockAuthUser = { id: 1, role };
      renderTab('queue');

      await waitFor(() => expect(screen.getAllByText(RECEIPT_NUMBER).length).toBeGreaterThan(0));
      expect(clearControls().length).toBeGreaterThan(0);
      // The withdrawn "ask someone else" hint must not render for any of them.
      expect(screen.queryAllByText(REMOVED_HINT)).toHaveLength(0);
    },
  );

  // The lockstep assertion, from the UI side: a supervisor sees Inspect AND Clear
  // Hold on the same row. Either control alone would pass a weaker test — the pair
  // is what proves the tier that can record a PASS can also record the truthful
  // "never needed inspecting", which is the whole reason the owner overruled the
  // earlier draft that withheld this from supervisors.
  it('renders BOTH Inspect and Clear Hold for a supervisor (the two gates are one list)', async () => {
    mockAuthUser = { id: 3, role: 'supervisor' };
    renderTab('queue');

    await waitFor(() => expect(screen.getAllByText(RECEIPT_NUMBER).length).toBeGreaterThan(0));
    expect(screen.getAllByRole('button', { name: /^inspect$/i }).length).toBeGreaterThan(0);
    expect(clearControls().length).toBeGreaterThan(0);
    // Nothing tells them to go ask a manager — they ARE the exit now.
    expect(screen.queryAllByText(REMOVED_HINT)).toHaveLength(0);
  });

  // The lockstep assertion generalized: NO role may reach this queue holding Inspect
  // and not Clear Hold. That one-way door is precisely what the deleted "ask a manager
  // or Quality" hint used to paper over, so it is asserted rather than commented.
  it.each(['admin', 'manager', 'supervisor', 'quality', 'platform_admin', 'operator', 'shipping', 'viewer'])(
    'never shows Inspect without Clear Hold for %s',
    async role => {
      mockAuthUser = { id: 5, role };
      renderTab('queue');

      await waitFor(() => expect(screen.getAllByText(RECEIPT_NUMBER).length).toBeGreaterThan(0));
      const inspects = screen.queryAllByRole('button', { name: /^inspect$/i }).length;
      if (inspects > 0) expect(clearControls().length).toBeGreaterThan(0);
    },
  );

  // The negative that keeps the gating assertions honest: an operator is below
  // BOTH gates, so neither control renders — while the row itself is on screen,
  // so a total render failure can't masquerade as correct gating.
  it('hides the control for an operator (below every receiving gate)', async () => {
    mockAuthUser = { id: 4, role: 'operator' };
    renderTab('queue');

    await waitFor(() => expect(screen.getAllByText(RECEIPT_NUMBER).length).toBeGreaterThan(0));
    expect(clearControls()).toHaveLength(0);
    // No Inspect either — there is no wrong path to steer them away from, so the
    // withdrawn hint would be noise on a row where nothing is actionable.
    expect(screen.queryAllByRole('button', { name: /^inspect$/i })).toHaveLength(0);
    expect(screen.queryAllByText(REMOVED_HINT)).toHaveLength(0);
  });

  // The visible label is the VERB. "Not Required" is already a StatusBadge value on
  // this page's History tab, so using it here would make one string an action in one
  // tab and a passive status in the other — and it says nothing about what clicking
  // does when it sits in a row reading Inspect / Correct / Void.
  it('labels the row control with the verb, not the status words', async () => {
    renderTab('queue');

    await waitFor(() => expect(clearControls().length).toBeGreaterThan(0));
    expect(clearControls()[0]).toHaveTextContent(/clear hold/i);
    expect(clearControls()[0]).not.toHaveTextContent(/not required/i);
  });
});

describe('Receiving — "Not Required" is queue-only', () => {
  it('does not render on history rows', async () => {
    renderTab('history');

    // History rows still carry their own Correct/Void cluster, so the actions
    // column definitely rendered — only the clear control is withheld.
    await waitFor(() => expect(screen.getAllByText('RCV-20260618-002').length).toBeGreaterThan(0));
    expect(screen.getAllByRole('button', { name: /correct receipt/i }).length).toBeGreaterThan(0);
    expect(clearControls()).toHaveLength(0);
  });

  it("does not render in a PO's receipt-history sub-table on the Receive tab", async () => {
    renderTab('receive');

    // Anchored: the card's accessible name STARTS with the PO number, while the
    // sibling delete control's is "Delete purchase order PO-2001". A bare
    // /PO-2001/ matches both and throws on multiple elements.
    fireEvent.click(await screen.findByRole('button', { name: /^PO-2001/ }));
    await waitFor(() => expect(mockApi.getPOForReceiving).toHaveBeenCalledWith(5));

    // The sub-table rendered (its Correct control for the sub-table's receipt is
    // present) but carries no clear-inspection control.
    expect(
      await screen.findByRole('button', { name: /correct receipt RCV-20260618-091/i }),
    ).toBeInTheDocument();
    expect(clearControls()).toHaveLength(0);
  });
});

describe('Receiving — clearing the inspection hold', () => {
  it('opens the dialog and blocks submit until a non-blank reason is entered', async () => {
    renderTab('queue');
    await openClearDialog();

    // Blank: InputDialog disables submit rather than firing a "reason required"
    // toast, so the API is unreachable.
    expect(screen.getByRole('button', { name: SUBMIT_LABEL })).toBeDisabled();
    submit();
    expect(mockApi.clearReceiptInspection).not.toHaveBeenCalled();

    // Whitespace-only trims to empty — still blocked.
    typeReason('   ');
    expect(screen.getByRole('button', { name: SUBMIT_LABEL })).toBeDisabled();
    submit();
    expect(mockApi.clearReceiptInspection).not.toHaveBeenCalled();

    // A real reason enables it.
    typeReason('Inspection box ticked by mistake.');
    expect(screen.getByRole('button', { name: SUBMIT_LABEL })).toBeEnabled();
  });

  it('submits the receipt id + TRIMMED reason, toasts success, and re-pulls the queue', async () => {
    mockApi.clearReceiptInspection.mockResolvedValueOnce(CLEARED);
    renderTab('queue');
    await openClearDialog();

    typeReason('  Stock hardware — no incoming inspection required.  ');
    submit();

    await waitFor(() =>
      expect(mockApi.clearReceiptInspection).toHaveBeenCalledWith(RECEIPT_ID, {
        reason: 'Stock hardware — no incoming inspection required.',
      }),
    );
    expect(await screen.findByText(/released to stock — no inspection required/i)).toBeInTheDocument();
    // Every receipt surface is re-pulled (initial load + refresh).
    await waitFor(() => expect(mockApi.getInspectionQueue.mock.calls.length).toBeGreaterThanOrEqual(2));
    await waitFor(() => expect(mockApi.getReceivingHistory).toHaveBeenCalled());
    // Dialog closed on success.
    await waitFor(() => expect(screen.queryByRole('heading', { name: DIALOG_TITLE })).toBeNull());
  });

  // History is a WINDOW on received_at (default 30 days) while the inspection queue
  // deliberately never ages out — so the receipts this verb exists for are precisely
  // the ones outside that window. Without widening it, a successful clear drops the
  // receipt off the queue AND out of History, leaving a success toast and nowhere to
  // confirm the material landed.
  it('widens the History window when the cleared hold is older than it', async () => {
    mockApi.getInspectionQueue.mockResolvedValue([{ ...QUEUE_ITEM, days_pending: 76 }]);
    mockApi.clearReceiptInspection.mockResolvedValueOnce(CLEARED);
    renderTab('queue');
    await openClearDialog();

    typeReason('Box ticked by mistake.');
    submit();

    await waitFor(() => expect(mockApi.getReceivingHistory).toHaveBeenCalledWith(77));
  });

  it('keeps the default 30-day History window for a fresh hold', async () => {
    mockApi.clearReceiptInspection.mockResolvedValueOnce(CLEARED);
    renderTab('queue');
    await openClearDialog();

    typeReason('Box ticked by mistake.');
    submit();

    await waitFor(() => expect(mockApi.clearReceiptInspection).toHaveBeenCalled());
    expect(mockApi.getReceivingHistory.mock.calls.every(call => call[0] === 30)).toBe(true);
  });

  it('surfaces a 409 detail verbatim, keeps the dialog open, and leaves the row', async () => {
    const detail = 'Receipt is not pending inspection';
    mockApi.clearReceiptInspection.mockRejectedValueOnce(http(409, detail));
    renderTab('queue');
    await openClearDialog();

    typeReason('Box ticked by mistake.');
    submit();

    expect(await screen.findByText(detail)).toBeInTheDocument();
    // No success toast, dialog still up so the reason can be adjusted, and the
    // row is untouched — the queue was never re-pulled.
    expect(screen.queryByText(/released to stock/i)).toBeNull();
    expect(screen.getByRole('heading', { name: DIALOG_TITLE })).toBeInTheDocument();
    expect(screen.getAllByText(RECEIPT_NUMBER).length).toBeGreaterThan(0);
    expect(mockApi.getInspectionQueue).toHaveBeenCalledTimes(1);
    // The typed reason survives the refusal — closing is the caller's job, so a
    // refused submit must not make the user retype it.
    expect(screen.getByLabelText(REASON_FIELD)).toHaveValue('Box ticked by mistake.');
    expect(screen.getByRole('button', { name: SUBMIT_LABEL })).toBeEnabled();
  });

  it('surfaces the 400 orphaned-PO-line detail verbatim', async () => {
    const detail =
      "Receipt's PO line no longer exists, so the inspection hold cannot be cleared here. " +
      'Contact an administrator to repair the receipt record.';
    mockApi.clearReceiptInspection.mockRejectedValueOnce(http(400, detail));
    renderTab('queue');
    await openClearDialog();

    typeReason('Box ticked by mistake.');
    submit();

    expect(await screen.findByText(detail)).toBeInTheDocument();
  });

  it('is NON-OPTIMISTIC: the row stays until the server answers', async () => {
    // Hold the call open, then let the refresh return an empty queue so the row
    // can only disappear as a consequence of the SERVER's answer.
    let resolveCall: (value: ClearedReceipt) => void = () => undefined;
    mockApi.clearReceiptInspection.mockImplementationOnce(
      () =>
        new Promise<ClearedReceipt>(resolve => {
          resolveCall = resolve;
        }),
    );
    mockApi.getInspectionQueue.mockResolvedValueOnce([QUEUE_ITEM]).mockResolvedValue([]);

    renderTab('queue');
    await openClearDialog();

    typeReason('Box ticked by mistake.');
    submit();
    await waitFor(() => expect(mockApi.clearReceiptInspection).toHaveBeenCalled());

    // In flight: nothing has moved. The row is still on the queue, the queue has
    // not been re-fetched, no success toast, and the dialog is still up with its
    // submit button in the guarded (loading) state.
    expect(screen.getAllByText(RECEIPT_NUMBER).length).toBeGreaterThan(0);
    expect(mockApi.getInspectionQueue).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/released to stock/i)).toBeNull();
    expect(screen.getByRole('heading', { name: DIALOG_TITLE })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: SUBMIT_LABEL })).toBeDisabled();

    resolveCall(CLEARED);

    // Only now does the row leave, and only because the refetch said so.
    await waitFor(() => expect(screen.queryAllByText(RECEIPT_NUMBER)).toHaveLength(0));
    expect(mockApi.getInspectionQueue.mock.calls.length).toBeGreaterThanOrEqual(2);
  });
});
