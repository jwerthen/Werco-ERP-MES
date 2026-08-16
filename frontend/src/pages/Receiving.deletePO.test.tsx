/**
 * Receiving — deleting an open PO from the "Receive Material" tab.
 *
 * The open-PO list is where a receiver actually meets a bad PO: a duplicate, a
 * mis-keyed line, an order that was cancelled by phone. Before this control the
 * only way off that list was to leave the page, find the PO in Purchasing, and
 * delete it there — so the wrong PO sat in the receiving queue until someone
 * bothered. The card now carries a delete control that calls the SAME endpoint
 * (DELETE /purchasing/purchase-orders/{id}), which is why this suite is written
 * against Purchasing.deleteRestore.test.tsx: one verb, two entry points, and
 * they must behave identically.
 *
 * Covered here (frontend contract only — the soft-delete, the audit row and the
 * received-material refusal are pinned by the backend suite):
 *  - RBAC parity with require_role([ADMIN, MANAGER]) on the endpoint: the control
 *    renders for those two and is ABSENT for supervisor, quality and operator —
 *    each negative asserting the PO card itself still rendered, so a page that
 *    failed to render at all cannot pass as correct gating;
 *  - the control is a SIBLING of the card button, not nested inside it: clicking
 *    it must not select the PO, or a receiver aiming at Delete would silently load
 *    a PO into the receive panel;
 *  - a confirm calls api.deletePurchaseOrder with THAT card's id (two POs are on
 *    screen, so a handler keyed on the wrong one is caught) and re-pulls the list;
 *  - STATE gating on top of role gating: the control is withheld from a PARTIAL PO.
 *    This list carries SENT and PARTIAL only, and PARTIAL means exactly "some line
 *    has quantity_received > 0" — the one condition the endpoint refuses on — so the
 *    control could only ever 400 there. Hiding it is also the safe direction: the
 *    refusal's own remedy ("void the receipt(s) first") destroys a real receipt and
 *    reverses posted stock when the material genuinely arrived;
 *  - THE COMMON CASE — a 400 "it has received material. Void the receipt(s) first"
 *    reaches the user VERBATIM and the card stays. Still reachable despite the
 *    PARTIAL gate above (a PO can go PARTIAL between list load and confirm). That
 *    sentence is the instruction the receiver has to follow next; a generic "delete
 *    failed" would strand them;
 *  - the dialog copy does NOT promise a restore from Purchasing, because no screen
 *    offers one;
 *  - the action is NON-OPTIMISTIC (house rule for server-gated actions): the card
 *    is still on screen, un-refetched, while the call is in flight;
 *  - deleting the PO that is loaded into the right-hand receive panel clears the
 *    selection — otherwise the panel keeps offering lines of a deleted PO and a
 *    receive posted against one fails confusingly.
 *
 * The api service + AuthContext are mocked at the module boundary; the real
 * ToastProvider wraps the page so toast text is assertable (sibling-test pattern
 * from Receiving.correctVoid.test.tsx / Receiving.clearInspection.test.tsx).
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ReceivingPage from './Receiving';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getOpenPOsForReceiving: jest.fn(),
    getReceivingLocations: jest.fn(),
    getReceivingStats: jest.fn(),
    getInspectionQueue: jest.fn(),
    getReceivingHistory: jest.fn(),
    getPOForReceiving: jest.fn(),
    deletePurchaseOrder: jest.fn(),
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

/** What api.deletePurchaseOrder actually resolves to — soft delete, hence can_restore. */
type DeleteResult = { message: string; can_restore: boolean };
const DELETED: DeleteResult = { message: 'Purchase order deleted', can_restore: true };

const poLine = (lineId: number) => ({
  line_id: lineId,
  line_number: 1,
  part_id: 7,
  part_number: 'PN-555',
  part_name: 'Bracket',
  quantity_ordered: 10,
  quantity_received: 0,
  quantity_remaining: 10,
  unit_price: 3.5,
  required_date: null,
  requires_inspection: false,
  is_closed: false,
});

const makePO = (poId: number, poNumber: string, vendorName = 'Acme Metals') => ({
  po_id: poId,
  po_number: poNumber,
  vendor_id: 1,
  vendor_name: vendorName,
  vendor_code: 'VND-001',
  order_date: null,
  required_date: null,
  expected_date: null,
  status: 'sent',
  lines: [poLine(poId * 10)],
  total_lines: 1,
});

// Two POs on screen: the id assertions below would pass on a handler wired to
// "the first card" if there were only one.
const PO_A = makePO(5, 'PO-2001');
const PO_B = makePO(6, 'PO-2002', 'Beta Alloys');
// A PO with material already received against it. The backend sets PARTIAL exactly
// when some line has quantity_received > 0, which is exactly what the delete endpoint
// refuses on — so the delete control must not render on this card.
const PO_PARTIAL = { ...makePO(7, 'PO-2003', 'Gamma Steel'), status: 'partial' };

const PANEL_PLACEHOLDER = /select a purchase order to view details/i;
const RECEIVED_MATERIAL_400 =
  'Cannot delete purchase order PO-2001: it has received material. Void the receipt(s) first, then delete.';

const renderReceiveTab = () =>
  render(
    <MemoryRouter initialEntries={['/receiving?tab=receive']}>
      <ToastProvider>
        <ReceivingPage />
      </ToastProvider>
    </MemoryRouter>,
  );

// The card button's accessible name STARTS with the PO number; the delete
// control's is "Delete purchase order <po>". Anchoring is what keeps the two
// apart — an unanchored /PO-2001/ matches both.
const poCard = (poNumber: string) => screen.getByRole('button', { name: new RegExp(`^${poNumber}`) });
const deleteControl = (poNumber: string) =>
  screen.queryByRole('button', { name: `Delete purchase order ${poNumber}` });

const dialog = () => screen.getByRole('dialog');
// Matched loosely on purpose: LoadingButton renders a Spinner carrying
// aria-label="Loading" INSIDE the button, so while the delete is in flight the
// confirm button's accessible name is "Loading Delete", not "Delete". An exact
// name would find it before the click and lose it exactly when the non-optimistic
// assertions need it. Cancel is the only other button in this dialog, so /delete/i
// stays unambiguous in both states.
const confirmButton = () => within(dialog()).getByRole('button', { name: /delete/i });
const cancelButton = () => within(dialog()).getByRole('button', { name: 'Cancel' });
const confirmDelete = () => fireEvent.click(confirmButton());

const openDeleteDialog = async (poNumber: string) => {
  await screen.findByText(poNumber);
  fireEvent.click(deleteControl(poNumber) as HTMLElement);
  return screen.findByRole('dialog');
};

beforeEach(() => {
  jest.clearAllMocks();
  mockAuthUser = { id: 1, role: 'manager' };
  mockApi.getOpenPOsForReceiving.mockResolvedValue([PO_A, PO_B]);
  mockApi.getReceivingLocations.mockResolvedValue([]);
  mockApi.getReceivingStats.mockResolvedValue({
    pending_inspection: 0,
    receipts_in_period: 0,
    acceptance_rate: 100,
    rejections_in_period: 0,
  });
  mockApi.getInspectionQueue.mockResolvedValue([]);
  mockApi.getReceivingHistory.mockResolvedValue([]);
  mockApi.getPOForReceiving.mockResolvedValue(PO_A);
});

describe('Receiving — open-PO delete RBAC gating', () => {
  // Mirrors require_role([ADMIN, MANAGER]) on DELETE /purchasing/purchase-orders/{id}
  // — the same gate Purchasing.tsx mirrors, and the same one canVoidReceipt uses.
  it.each(['admin', 'manager'])('renders a delete control on every open-PO card for %s', async role => {
    mockAuthUser = { id: 1, role };
    renderReceiveTab();

    await screen.findByText('PO-2001');
    expect(deleteControl('PO-2001')).toBeInTheDocument();
    expect(deleteControl('PO-2002')).toBeInTheDocument();
  });

  // State gate, not a role gate: an ADMIN — the most privileged tenant role — still
  // gets no control on a PARTIAL card, and keeps it on the SENT ones beside it, so
  // this cannot pass by accident on a page that rendered no controls at all.
  it.each(['admin', 'manager'])('withholds the delete control from a PARTIAL PO for %s', async role => {
    mockAuthUser = { id: 1, role };
    mockApi.getOpenPOsForReceiving.mockResolvedValue([PO_A, PO_PARTIAL]);
    renderReceiveTab();

    await screen.findByText('PO-2003');
    expect(poCard('PO-2003')).toBeInTheDocument();
    expect(deleteControl('PO-2003')).toBeNull();
    // The SENT card beside it still has one.
    expect(deleteControl('PO-2001')).toBeInTheDocument();
  });

  // Each negative asserts the CARD is on screen first. Without that, a page that
  // crashed or rendered an empty list would satisfy "no delete control" and this
  // test would report correct gating for a broken screen.
  it.each(['supervisor', 'quality', 'operator'])(
    'withholds the delete control from %s while the PO card still renders',
    async role => {
      mockAuthUser = { id: 9, role };
      renderReceiveTab();

      await screen.findByText('PO-2001');
      expect(poCard('PO-2001')).toBeInTheDocument();
      expect(poCard('PO-2002')).toBeInTheDocument();
      expect(deleteControl('PO-2001')).toBeNull();
      expect(deleteControl('PO-2002')).toBeNull();
      expect(mockApi.deletePurchaseOrder).not.toHaveBeenCalled();
    },
  );
});

describe('Receiving — deleting an open PO', () => {
  it('does not select the PO — the receive panel is untouched while the dialog is up', async () => {
    renderReceiveTab();
    await openDeleteDialog('PO-2001');

    // The card's own onClick never fired: no detail fetch, and the right-hand
    // panel still shows its "nothing selected" placeholder.
    expect(mockApi.getPOForReceiving).not.toHaveBeenCalled();
    expect(screen.getByText(PANEL_PLACEHOLDER)).toBeInTheDocument();

    // The dialog names the PO and its vendor, and says what the SOFT delete actually
    // buys the user. It must NOT promise a restore from Purchasing: the endpoint and
    // api.restorePurchaseOrder exist, but nothing in the frontend calls that wrapper
    // and Purchasing has no restore control or deleted-PO view, so the only way back
    // is an administrator against the API. The negative below is the load-bearing
    // assertion — a manager who believes the delete is one click from reversible is
    // exactly the manager who deletes the live PO without checking.
    const copy = within(dialog()).getByText(/Delete purchase order PO-2001 from Acme Metals\?/);
    expect(copy).toHaveTextContent(/no restore button in the app/i);
    expect(copy).toHaveTextContent(/administrator action/i);
    expect(copy).not.toHaveTextContent(/restore it from Purchasing/i);
    // Both halves of the received-material advice. The server's 400 supplies only the
    // first ("void the receipt(s) first"), which is destructive guidance when the
    // material genuinely arrived — the dialog is where the counter-guidance lives.
    expect(copy).toHaveTextContent(/void the receipt\(s\) first/i);
    expect(copy).toHaveTextContent(/close or cancel the PO in Purchasing instead/i);
  });

  it('closes on Cancel without calling the API', async () => {
    renderReceiveTab();
    await openDeleteDialog('PO-2001');

    fireEvent.click(cancelButton());

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(mockApi.deletePurchaseOrder).not.toHaveBeenCalled();
    expect(poCard('PO-2001')).toBeInTheDocument();
  });

  it('confirms with THAT card\'s id, toasts success, and re-pulls the open-PO list', async () => {
    mockApi.deletePurchaseOrder.mockResolvedValueOnce(DELETED);
    // The refreshed list is what removes the card — the UI never removes it itself.
    mockApi.getOpenPOsForReceiving.mockResolvedValueOnce([PO_A, PO_B]).mockResolvedValue([PO_A]);
    renderReceiveTab();

    // Delete the SECOND card: an id taken from the wrong card would send 5 here.
    await openDeleteDialog('PO-2002');
    confirmDelete();

    await waitFor(() => expect(mockApi.deletePurchaseOrder).toHaveBeenCalledWith(6));
    expect(mockApi.deletePurchaseOrder).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/Purchase order PO-2002 deleted/)).toBeInTheDocument();
    // Re-pulled (initial load + post-delete refresh), and the card is gone because
    // the SERVER's list no longer carries it.
    await waitFor(() => expect(mockApi.getOpenPOsForReceiving.mock.calls.length).toBeGreaterThanOrEqual(2));
    await waitFor(() => expect(screen.queryByText('PO-2002')).toBeNull());
    // Dialog closed on success.
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  // The common real-world refusal: the PO has material received against it, so the
  // server refuses and hands back the exact remedy. Anything less than verbatim
  // leaves the receiver with a PO they cannot delete and no idea why.
  it('surfaces the 400 received-material detail VERBATIM and leaves the card in place', async () => {
    mockApi.deletePurchaseOrder.mockRejectedValueOnce(http(400, RECEIVED_MATERIAL_400));
    renderReceiveTab();

    await openDeleteDialog('PO-2001');
    confirmDelete();

    await waitFor(() => expect(mockApi.deletePurchaseOrder).toHaveBeenCalledWith(5));
    expect(await screen.findByText(RECEIVED_MATERIAL_400)).toBeInTheDocument();
    // The PO is still there, no success toast was fired, the list was never
    // re-pulled, and the dialog stays up carrying the server's own wording.
    expect(poCard('PO-2001')).toBeInTheDocument();
    expect(screen.queryByText(/deleted — record retained for audit/)).toBeNull();
    expect(mockApi.getOpenPOsForReceiving).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('surfaces a generic failure message when the server sends no detail', async () => {
    mockApi.deletePurchaseOrder.mockRejectedValueOnce(http(500));
    renderReceiveTab();

    await openDeleteDialog('PO-2001');
    confirmDelete();

    expect(await screen.findByText('Failed to delete purchase order')).toBeInTheDocument();
    expect(poCard('PO-2001')).toBeInTheDocument();
  });

  it('is NON-OPTIMISTIC: the card stays until the server answers', async () => {
    // Hold the call open, then let the refresh return a list without the PO, so the
    // card can only disappear as a consequence of the SERVER's answer.
    let resolveCall: (value: DeleteResult) => void = () => undefined;
    mockApi.deletePurchaseOrder.mockImplementationOnce(
      () =>
        new Promise<DeleteResult>(resolve => {
          resolveCall = resolve;
        }),
    );
    mockApi.getOpenPOsForReceiving.mockResolvedValueOnce([PO_A, PO_B]).mockResolvedValue([PO_B]);
    renderReceiveTab();

    await openDeleteDialog('PO-2001');
    confirmDelete();
    await waitFor(() => expect(mockApi.deletePurchaseOrder).toHaveBeenCalledWith(5));

    // In flight: nothing has moved. The card is still on the list, the list has not
    // been re-fetched, no success toast, and the dialog is still up with its confirm
    // button in the guarded (loading) state.
    expect(poCard('PO-2001')).toBeInTheDocument();
    expect(mockApi.getOpenPOsForReceiving).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/deleted — record retained for audit/)).toBeNull();
    expect(confirmButton()).toBeDisabled();
    expect(cancelButton()).toBeDisabled();

    resolveCall(DELETED);

    // Only now does the card leave, and only because the refetch said so.
    await waitFor(() => expect(screen.queryByText('PO-2001')).toBeNull());
    expect(mockApi.getOpenPOsForReceiving.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it('clears the receive panel when the DELETED PO was the selected one', async () => {
    mockApi.deletePurchaseOrder.mockResolvedValueOnce(DELETED);
    renderReceiveTab();

    // Select PO-2001 into the right-hand panel first.
    await screen.findByText('PO-2001');
    fireEvent.click(poCard('PO-2001'));
    await waitFor(() => expect(mockApi.getPOForReceiving).toHaveBeenCalledWith(5));
    await waitFor(() => expect(screen.queryByText(PANEL_PLACEHOLDER)).toBeNull());

    fireEvent.click(deleteControl('PO-2001') as HTMLElement);
    await screen.findByRole('dialog');
    confirmDelete();

    await waitFor(() => expect(mockApi.deletePurchaseOrder).toHaveBeenCalledWith(5));
    // selectedPO comes from its OWN getPOForReceiving fetch, so refreshing the open
    // list does not clear it — the handler has to. The mocked list deliberately still
    // returns PO-2001, so this asserts the SELECTION was dropped and not merely that
    // the PO left the list.
    expect(await screen.findByText(PANEL_PLACEHOLDER)).toBeInTheDocument();
  });

  it('leaves an UNRELATED selection alone', async () => {
    mockApi.deletePurchaseOrder.mockResolvedValueOnce(DELETED);
    renderReceiveTab();

    await screen.findByText('PO-2001');
    fireEvent.click(poCard('PO-2001'));
    await waitFor(() => expect(mockApi.getPOForReceiving).toHaveBeenCalledWith(5));

    // Delete the OTHER PO — the loaded panel must survive it.
    fireEvent.click(deleteControl('PO-2002') as HTMLElement);
    await screen.findByRole('dialog');
    confirmDelete();

    await waitFor(() => expect(mockApi.deletePurchaseOrder).toHaveBeenCalledWith(6));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.queryByText(PANEL_PLACEHOLDER)).toBeNull();
  });
});
