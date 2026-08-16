/**
 * Purchasing — PO / vendor soft-delete controls, and the restore that closes the loop.
 *
 * Covers the delete feature wired into the Purchasing page:
 *  - a PO Delete action opens the confirm dialog and, on confirm, calls
 *    api.deletePurchaseOrder with the PO id;
 *  - a server 400 (received material) leaves the row in place and surfaces the
 *    verbatim `detail` in an error toast (no success toast, no dead state);
 *  - a Vendor Delete action calls api.deleteVendor with the vendor id;
 *  - the Delete controls are hidden for a role below the [admin, manager] gate
 *    (RBAC parity with the backend require_role), so no button 403s.
 *
 * ...and the RESTORE half, added once the page grew a deleted-PO view.
 *
 * The restore VERB (POST /purchasing/purchase-orders/{id}/restore) and its client
 * wrapper both predate this UI by months; what was missing was DISCOVERY. Every read
 * of the list endpoint hard-filtered `is_deleted == False`, so a soft-deleted PO was
 * invisible to every caller and there was nothing a Restore button could act on. The
 * fix is one query param — `deleted_only` — and the tests below are written around
 * that fact, so they check the two halves separately:
 *
 *   1. DISCOVERY. Switching to the Deleted view calls getPurchaseOrders with
 *      `deleted_only: true`, and — the frozen half of the contract — the default page
 *      load does NOT send the key at all. A param that leaked onto the normal path
 *      would change what the live list returns, which is the one thing this feature
 *      was not allowed to do.
 *   2. THE VERB. Right id, success toast, refresh, verbatim refusal, non-optimistic
 *      ordering, and RBAC parity with require_role([ADMIN, MANAGER]).
 *
 * Two properties get more attention than their line count suggests:
 *
 *  - THE TWO BOOKS ARE SEPARATE. A deleted PO is a record; a live one is workable.
 *    They render from different state, in mutually exclusive views, and the deleted
 *    rows carry neither Print nor Send nor row click-through — because the active
 *    table's row click PRINTS the PO, and the worst thing this page could do is help
 *    someone mail a dead order to a vendor. Asserted as ABSENCES on the deleted rows,
 *    since that is the direction that fails dangerously.
 *  - THE VIEW IS NOT ROLE-GATED, THE BUTTON IS. Deliberate, and mirrored from the
 *    server: the list endpoint stays on get_current_user because `deleted_only` returns
 *    rows the same reader could already see before the delete, so no new gate; only the
 *    verb is privileged. The role tests below therefore assert that a supervisor can
 *    still OPEN the archive and read it, and only lacks the button.
 *
 * Every negative here is anchored on a positive (the deleted row itself is asserted
 * present) so that a page which crashed or rendered nothing cannot pass as correct
 * gating — same discipline as the sibling Receiving.deletePO suite.
 *
 * The api service + AuthContext are mocked at the module boundary; the real
 * ToastProvider wraps the page so the error toast text is assertable (same
 * pattern as the sibling Purchasing tests).
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import { formatCentralDateTime } from '../utils/centralTime';
import Purchasing from './Purchasing';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getVendors: jest.fn(),
    getPurchaseOrders: jest.fn(),
    getParts: jest.fn(),
    deletePurchaseOrder: jest.fn(),
    deleteVendor: jest.fn(),
    restorePurchaseOrder: jest.fn(),
  },
}));

let mockAuthUser: { id: number; role: string } = { id: 1, role: 'manager' };
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockAuthUser, isAuthenticated: true, isLoading: false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const http = (status: number, detail?: string) => {
  const err = new Error(detail || 'error') as Error & {
    response: { status: number; data: { detail?: string } };
  };
  err.response = { status, data: { detail } };
  return err;
};

const vendors = [
  { id: 5, code: 'VND-005', name: 'Acme Aerospace', is_approved: true, is_active: true, version: 0 },
];

/**
 * The list endpoint's row shape. The soft-delete provenance fields are OPTIONAL and
 * come back only from the `deleted_only=true` read — undefined on every live row, which
 * is exactly how one shared row type serves both books. `deleted_by_name` is nullable
 * rather than merely absent: the server resolves it from the User row, and that row can
 * be gone (a departed employee), which is a case the UI has to render honestly.
 */
type PORow = {
  id: number;
  po_number: string;
  vendor_id: number;
  vendor_name: string;
  status: string;
  order_date: string;
  required_date: string;
  total: number;
  line_count: number;
  is_deleted?: boolean;
  deleted_at?: string;
  deleted_by_name?: string | null;
};

const purchaseOrders: PORow[] = [
  {
    id: 10,
    po_number: 'PO-2001',
    vendor_id: 5,
    vendor_name: 'Acme Aerospace',
    status: 'sent',
    order_date: '2026-06-20',
    required_date: '2026-07-01',
    total: 1250.5,
    line_count: 3,
  },
];

// Deliberately straddles the UTC/Central date boundary: 02:30Z on Aug 14 is 9:30 PM on
// Aug 13 in Chicago. A page that formatted the raw instant without converting would
// render "Aug 14" and fail — which is the whole point of picking this value.
const DELETED_AT_3001 = '2026-08-14T02:30:00Z';
const DELETED_AT_3002 = '2026-08-12T15:05:00Z';

/**
 * The deleted book.
 *
 * PO-3002 is CANCELLED on purpose. The live list excludes CLOSED/CANCELLED when no
 * explicit status filter is given, and the deleted view must NOT inherit that exclusion:
 * a PO that was cancelled and then deleted is one of the likeliest things anyone wants
 * back, and hiding it would leave the Restore button unable to reach it. The server owns
 * that carve-out; what this file can pin is that the page renders such a row rather than
 * filtering it out again on the client.
 *
 * PO-3002 also carries `deleted_by_name: null` — the departed-user case.
 */
const deletedPurchaseOrders: PORow[] = [
  {
    id: 31,
    po_number: 'PO-3001',
    vendor_id: 5,
    vendor_name: 'Acme Aerospace',
    status: 'sent',
    order_date: '2026-07-02',
    required_date: '2026-07-20',
    total: 880.25,
    line_count: 2,
    is_deleted: true,
    deleted_at: DELETED_AT_3001,
    deleted_by_name: 'Dana Ruiz',
  },
  {
    id: 32,
    po_number: 'PO-3002',
    vendor_id: 5,
    vendor_name: 'Beta Alloys',
    status: 'cancelled',
    order_date: '2026-07-05',
    required_date: '2026-07-25',
    total: 410,
    line_count: 1,
    is_deleted: true,
    deleted_at: DELETED_AT_3002,
    deleted_by_name: null,
  },
];

// Stands in for the server's copy of the deleted book so a restore can actually change
// what the next read returns. Reassigned per-test; read by the getPurchaseOrders mock.
let deletedBook: PORow[] = [];

const renderPurchasing = () =>
  render(
    <MemoryRouter>
      <ToastProvider>
        <Purchasing />
      </ToastProvider>
    </MemoryRouter>,
  );

// The confirm dialog's "Delete" button (portaled to document.body as role=dialog),
// distinct from the row-level "Delete" trigger.
const confirmDialogDeleteButton = () =>
  within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' });

// The Active/Deleted switch, scoped to its own role="group". Scoping is REQUIRED, not
// tidiness: DataTable renders every sortable column header as a <button>, so once the
// deleted table is up there is a second button whose accessible name is exactly
// "Deleted" (the column header). An unscoped getByRole would be ambiguous.
const viewSwitch = () => within(screen.getByRole('group', { name: 'Purchase order view' }));
const viewButton = (label: 'Active' | 'Deleted') => viewSwitch().getByRole('button', { name: label });

// DataTable renders BOTH the desktop table and the mobile cards in jsdom (`md:hidden`
// is CSS-only), so every row-level control exists twice. All row queries are plural.
const restoreButtons = (poNumber: string) =>
  screen.queryAllByRole('button', { name: `Restore purchase order ${poNumber}` });
const allRestoreButtons = () => screen.queryAllByRole('button', { name: /^Restore purchase order/ });

/** The desktop <tr> for a PO — used to assert the row-level dimming on deleted rows. */
const tableRowFor = (poNumber: string): HTMLTableRowElement => {
  const row = screen
    .getAllByText(poNumber)
    .map((el) => el.closest('tr'))
    .find((tr): tr is HTMLTableRowElement => tr !== null);
  if (!row) throw new Error(`No table row rendered for ${poNumber}`);
  return row;
};

/** Open the Deleted view and wait for its rows. */
const openDeletedView = async () => {
  fireEvent.click(viewButton('Deleted'));
  await screen.findAllByText('PO-3001');
};

/** Every getPurchaseOrders call that asked for the deleted book. */
const deletedReads = () =>
  mockedApi.getPurchaseOrders.mock.calls.filter(([params]) => params?.deleted_only === true);

beforeEach(() => {
  jest.clearAllMocks();
  mockAuthUser = { id: 1, role: 'manager' };
  deletedBook = deletedPurchaseOrders;
  mockedApi.getVendors.mockResolvedValue(vendors as any);
  // Answers as the endpoint does: the SAME route serves both books, and `deleted_only`
  // is the only thing that tells them apart.
  mockedApi.getPurchaseOrders.mockImplementation(async (params) =>
    params?.deleted_only ? deletedBook : purchaseOrders,
  );
  mockedApi.getParts.mockResolvedValue([] as any);
});

describe('Purchasing — PO delete', () => {
  test('confirm dialog calls api.deletePurchaseOrder with the PO id', async () => {
    mockedApi.deletePurchaseOrder.mockResolvedValueOnce({ message: 'deleted', can_restore: true });
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

    // Row-level Delete (the desktop DataTable + mobile card both render one; either
    // opens the same confirm dialog). Click the first, then confirm in the dialog.
    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);
    fireEvent.click(confirmDialogDeleteButton());

    await waitFor(() => expect(mockedApi.deletePurchaseOrder).toHaveBeenCalledWith(10));
  });

  test('a 400 (received material) surfaces the verbatim detail toast and keeps the row', async () => {
    const detail = 'Cannot delete purchase order PO-2001: it has received material. Void the receipt(s) first, then delete.';
    mockedApi.deletePurchaseOrder.mockRejectedValueOnce(http(400, detail));
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);
    fireEvent.click(confirmDialogDeleteButton());

    await waitFor(() => expect(mockedApi.deletePurchaseOrder).toHaveBeenCalledWith(10));
    // Verbatim server detail in an error toast; the PO row is still present.
    expect(await screen.findByText(detail)).toBeInTheDocument();
    expect(screen.getAllByText('PO-2001').length).toBeGreaterThan(0);
  });
});

describe('Purchasing — vendor delete', () => {
  test('confirm dialog calls api.deleteVendor with the vendor id', async () => {
    mockedApi.deleteVendor.mockResolvedValueOnce({ message: 'deleted', can_restore: true });
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

    // Switch to the Vendors tab, then delete the vendor.
    fireEvent.click(screen.getByRole('button', { name: /vendors/i }));
    await screen.findByText('VND-005');
    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);
    fireEvent.click(confirmDialogDeleteButton());

    await waitFor(() => expect(mockedApi.deleteVendor).toHaveBeenCalledWith(5));
  });
});

describe('Purchasing — RBAC gating', () => {
  test('Delete controls are hidden for a supervisor (below the admin/manager gate)', async () => {
    mockAuthUser = { id: 9, role: 'supervisor' };
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

    // No PO Delete trigger on the orders tab.
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull();

    // ...and none on the vendors tab either.
    fireEvent.click(screen.getByRole('button', { name: /vendors/i }));
    await screen.findByText('VND-005');
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull();
    expect(mockedApi.deletePurchaseOrder).not.toHaveBeenCalled();
    expect(mockedApi.deleteVendor).not.toHaveBeenCalled();
  });
});

/**
 * DISCOVERY — the half that did not exist before.
 *
 * The restore endpoint and api.restorePurchaseOrder were both already here; what was
 * missing was any way to SEE a soft-deleted PO, because every read of the list endpoint
 * filtered `is_deleted == False`. These tests pin the new query param in both
 * directions, and the "inert when unset" one is the non-negotiable half: it guards the
 * live purchasing list, which this feature was not allowed to change.
 */
describe('Purchasing — the deleted-PO view', () => {
  test('the default page load does NOT send deleted_only — the live list is untouched', async () => {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

    // Called with NO arguments at all, exactly as before the param existed. Asserted as
    // zero-args rather than `{ deleted_only: false }` on purpose: axios omits undefined
    // keys, so an absent key is what keeps the query string byte-identical, and sending
    // the key explicitly — even as false — would be a change to the live read.
    expect(mockedApi.getPurchaseOrders).toHaveBeenCalledWith();
    expect(deletedReads()).toHaveLength(0);
    // The active book is what rendered.
    expect(screen.getAllByText('PO-2001').length).toBeGreaterThan(0);
    expect(screen.queryAllByText('PO-3001')).toHaveLength(0);
  });

  test('switching to Deleted fetches with deleted_only and shows only the deleted book', async () => {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

    await openDeletedView();

    expect(mockedApi.getPurchaseOrders).toHaveBeenCalledWith({ deleted_only: true });
    expect(deletedReads()).toHaveLength(1);
    // Both deleted POs are here, and the live one is NOT — two separate books, never
    // merged into one table where a dead order could be mistaken for workable.
    expect(screen.getAllByText('PO-3001').length).toBeGreaterThan(0);
    expect(screen.getAllByText('PO-3002').length).toBeGreaterThan(0);
    expect(screen.queryAllByText('PO-2001')).toHaveLength(0);
  });

  // A CANCELLED PO that was then deleted is the likeliest thing anyone wants back. The
  // live list hides CLOSED/CANCELLED by default and the deleted view must not inherit
  // that, or Restore could never reach exactly the rows people go looking for.
  test('renders a CANCELLED deleted PO — the default status exclusion is not re-applied', async () => {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedView();

    expect(screen.getAllByText('PO-3002').length).toBeGreaterThan(0);
    expect(restoreButtons('PO-3002').length).toBeGreaterThan(0);
  });

  // Every entry re-reads, not just the first: somebody else may have deleted or restored
  // in the meantime, and a stale archive is how you click Restore on a row the server
  // will refuse.
  test('re-fetches the deleted book on every entry into the view', async () => {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

    await openDeletedView();
    expect(deletedReads()).toHaveLength(1);

    fireEvent.click(viewButton('Active'));
    await screen.findAllByText('PO-2001');
    await openDeletedView();

    expect(deletedReads()).toHaveLength(2);
  });
});

describe('Purchasing — deleted rows are distinguishable from live ones', () => {
  test('the deleted view is labelled, banner-marked, dimmed, and states the two views apart', async () => {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchase Orders' });
    await openDeletedView();

    // Heading flips, and the switch reports which book you are in to assistive tech.
    expect(screen.getByRole('heading', { name: 'Deleted Purchase Orders' })).toBeInTheDocument();
    expect(viewButton('Deleted')).toHaveAttribute('aria-pressed', 'true');
    expect(viewButton('Active')).toHaveAttribute('aria-pressed', 'false');

    // A notice strip says what these rows ARE — records, off the receiving list.
    const banner = screen.getByText(/These purchase orders are deleted records/i);
    expect(banner).toHaveTextContent(/nothing can be received against them/i);

    // Rows are visually dimmed, not merely differently laid out.
    expect(tableRowFor('PO-3001').className).toMatch(/opacity-70/);

    // Provenance columns exist only here.
    expect(screen.getByRole('columnheader', { name: /Deleted By/i })).toBeInTheDocument();
  });

  // The dangerous direction. The active table's row click PRINTS the PO, and Print/Send
  // would hand a vendor an order that no longer exists — so a deleted row carries none
  // of the three, and Restore is the only affordance on it.
  test('deleted rows carry NO Print, Send or Delete — only Restore', async () => {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedView();

    expect(screen.queryAllByRole('button', { name: 'Print' })).toHaveLength(0);
    expect(screen.queryAllByRole('button', { name: 'Send' })).toHaveLength(0);
    expect(screen.queryAllByRole('button', { name: 'Delete' })).toHaveLength(0);
    // ...and the row is genuinely on screen, so the absences above mean something.
    expect(screen.getAllByText('PO-3001').length).toBeGreaterThan(0);
    expect(restoreButtons('PO-3001').length).toBeGreaterThan(0);
  });

  // The mirror image: nothing on the LIVE book offers a restore, which would be a
  // control that can only ever 400 (the endpoint refuses a PO that is not deleted).
  test('the live view shows no restore control', async () => {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

    expect(screen.getAllByText('PO-2001').length).toBeGreaterThan(0);
    expect(screen.getAllByRole('button', { name: 'Delete' }).length).toBeGreaterThan(0);
    expect(allRestoreButtons()).toHaveLength(0);
  });

  // `deleted_at` is UTC over the wire and MUST render in shop-local Central. The fixture
  // instant is 02:30Z on Aug 14, which is Aug 13 in Chicago — so a page that formatted
  // the raw instant, or leaned on the viewer's timezone, lands on the wrong DAY.
  //
  // BOTH renderers are pinned, and that is not belt-and-braces: DataTable emits the
  // desktop table AND the mobile cards, so a `toLocaleString()` in one of them is
  // invisible to any assertion the other can satisfy. An earlier version of this test
  // used getAllByText(...).length > 0 and passed with the desktop cell fully broken.
  test('renders deleted_at through the Central-time helper in BOTH renderers, never the raw ISO', async () => {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedView();

    const central = formatCentralDateTime(DELETED_AT_3001);
    // Guard on the fixture itself: if this ever stops straddling the date boundary the
    // assertions below quietly stop proving anything.
    expect(central).toMatch(/Aug 13/);

    // The desktop cell, named explicitly so a failure says WHICH renderer drifted.
    expect(within(tableRowFor('PO-3001')).getByText(central)).toBeInTheDocument();
    // ...and exactly the two elements DataTable renders per row carry it, so the mobile
    // twin cannot quietly format the same instant a different way.
    expect(screen.getAllByText(central)).toHaveLength(2);

    // The raw wire value never reaches the screen, and neither does the UTC calendar day
    // nor a locale-default `toLocaleString()` shape (which would render "8/13/2026,
    // 9:30:00 PM" — right day, wrong function, and wrong for any viewer outside Central).
    expect(screen.queryAllByText(DELETED_AT_3001)).toHaveLength(0);
    expect(screen.queryAllByText(/Aug 14, 2026/)).toHaveLength(0);
    expect(screen.queryAllByText(/^\d{1,2}\/\d{1,2}\/\d{4}/)).toHaveLength(0);

    // The departed-user case: deleted_by_name comes back null and the cell says so
    // rather than rendering blank. Table cell scoped for the same reason as above.
    expect(within(tableRowFor('PO-3001')).getByText('Dana Ruiz')).toBeInTheDocument();
    expect(within(tableRowFor('PO-3002')).getByText('Unknown')).toBeInTheDocument();
  });
});

describe('Purchasing — restoring a deleted PO', () => {
  test('calls api.restorePurchaseOrder with THAT row\'s id, toasts, and re-reads both books', async () => {
    // The server-side effect of a successful restore: the PO leaves the deleted book.
    mockedApi.restorePurchaseOrder.mockImplementationOnce(async () => {
      deletedBook = [deletedPurchaseOrders[0]];
      return { message: 'Purchase order restored' };
    });
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedView();

    const activeReadsBefore = mockedApi.getPurchaseOrders.mock.calls.length - deletedReads().length;

    // Restore the SECOND row: an id taken from "the first row" would send 31 here.
    fireEvent.click(restoreButtons('PO-3002')[0]);

    await waitFor(() => expect(mockedApi.restorePurchaseOrder).toHaveBeenCalledWith(32));
    expect(mockedApi.restorePurchaseOrder).toHaveBeenCalledTimes(1);
    // PO-3002 is CANCELLED, so the toast is the WARNING variant that says where it went —
    // see the dedicated pair of tests below.
    expect(await screen.findByText(/Purchase order PO-3002 restored/)).toBeInTheDocument();

    // BOTH books are re-read — the deleted one so the row leaves, the active one so it
    // reappears where it belongs.
    await waitFor(() => expect(deletedReads()).toHaveLength(2));
    const activeReadsAfter = mockedApi.getPurchaseOrders.mock.calls.length - deletedReads().length;
    expect(activeReadsAfter).toBeGreaterThan(activeReadsBefore);

    // Wait for the table itself to come back (the refresh flips the page to its loading
    // state briefly), THEN assert the row is gone — otherwise "absent" would also be
    // satisfied mid-refresh, when nothing at all is rendered.
    await screen.findAllByText('PO-3001');
    expect(screen.queryAllByText('PO-3002')).toHaveLength(0);
  });

  // Server-GATED: a 400 here is the normal case (the PO was already restored by someone
  // else). The user needs the server's own sentence, and the row must not move.
  test('a refusal surfaces the verbatim detail and LEAVES the row in the deleted view', async () => {
    const detail = 'Purchase order PO-3001 is not deleted.';
    mockedApi.restorePurchaseOrder.mockRejectedValueOnce(http(400, detail));
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedView();

    fireEvent.click(restoreButtons('PO-3001')[0]);

    await waitFor(() => expect(mockedApi.restorePurchaseOrder).toHaveBeenCalledWith(31));
    expect(await screen.findByText(detail)).toBeInTheDocument();

    // No success toast, and the row is exactly where it was — nothing moved on the
    // strength of a refusal.
    expect(screen.queryByText(/PO-3001 restored/)).toBeNull();
    expect(screen.getAllByText('PO-3001').length).toBeGreaterThan(0);
    expect(restoreButtons('PO-3001').length).toBeGreaterThan(0);

    // ...but the archive IS re-read. A 400 here means the server disagrees with what this
    // table shows (overwhelmingly: somebody else already restored it), so the list must
    // converge on the server's answer instead of keeping a phantom row whose every click
    // reproduces the same refusal. Still non-optimistic — the row can only move because a
    // fresh server response moved it.
    await waitFor(() => expect(deletedReads()).toHaveLength(2));
  });

  test('falls back to a generic message when the server sends no detail, and does NOT re-read', async () => {
    mockedApi.restorePurchaseOrder.mockRejectedValueOnce(http(500));
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedView();

    fireEvent.click(restoreButtons('PO-3001')[0]);

    expect(await screen.findByText('Failed to restore purchase order')).toBeInTheDocument();
    expect(screen.getAllByText('PO-3001').length).toBeGreaterThan(0);
    // The other half of the re-read rule: a 5xx / offline failure tells us nothing about
    // this row, and a re-read that also failed would swap the archive for an error state
    // over a transient blip. Only a 4xx — the server disagreeing with the list — re-reads.
    expect(deletedReads()).toHaveLength(1);
  });

  // House rule for server-gated actions. Nothing may move before the server answers, or
  // the UI shows a state the server might refuse.
  test('is NON-OPTIMISTIC: the row stays put until the server answers', async () => {
    let resolveRestore: (value: { message: string }) => void = () => undefined;
    mockedApi.restorePurchaseOrder.mockImplementationOnce(
      () =>
        new Promise<{ message: string }>((resolve) => {
          resolveRestore = resolve;
        }),
    );
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedView();

    fireEvent.click(restoreButtons('PO-3001')[0]);
    await waitFor(() => expect(mockedApi.restorePurchaseOrder).toHaveBeenCalledWith(31));

    // In flight: the row has not moved, neither book has been re-read, and no success
    // toast has fired.
    expect(screen.getAllByText('PO-3001').length).toBeGreaterThan(0);
    expect(deletedReads()).toHaveLength(1);
    expect(screen.queryByText(/PO-3001 restored/)).toBeNull();

    // The clicked row carries the spinner + its loading label; every OTHER Restore is
    // merely disabled, so a second restore cannot be fired underneath the first.
    // Asserted across ALL of each row's buttons, not just [0]: the desktop table and the
    // mobile cards both render one, and a twin that stayed enabled would be a live
    // double-submit that an index-0 assertion cannot see.
    expect(restoreButtons('PO-3001')).toHaveLength(2);
    restoreButtons('PO-3001').forEach((btn) => {
      expect(btn).toBeDisabled();
      expect(btn).toHaveTextContent('Restoring…');
    });
    expect(restoreButtons('PO-3002')).toHaveLength(2);
    restoreButtons('PO-3002').forEach((btn) => {
      expect(btn).toBeDisabled();
      expect(btn).not.toHaveTextContent('Restoring…');
    });

    // Only the server's answer moves it.
    deletedBook = [deletedPurchaseOrders[1]];
    resolveRestore({ message: 'Purchase order restored' });

    await waitFor(() => expect(deletedReads()).toHaveLength(2));
    await screen.findAllByText('PO-3002');
    expect(screen.queryAllByText('PO-3001')).toHaveLength(0);
  });
});

/**
 * RESTORED, BUT WHERE DID IT GO?
 *
 * The deleted view exists without the default closed/cancelled exclusion precisely so a
 * CANCELLED-then-deleted PO can be found and restored. The active view still applies that
 * exclusion — `getPurchaseOrders()` with no status — so restoring such a PO takes it out
 * of the Deleted book without putting it in the Active one. The server is right on both
 * counts; what would be wrong is telling the user "restored" and leaving them to discover
 * the record on no list they can open. That is what the `warning` variant is for.
 */
describe('Purchasing — restoring a PO the active list will not show', () => {
  test('a CANCELLED PO restores with a WARNING toast naming where it went', async () => {
    mockedApi.restorePurchaseOrder.mockImplementationOnce(async () => {
      deletedBook = [deletedPurchaseOrders[0]];
      return { message: 'Purchase order restored' };
    });
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedView();

    fireEvent.click(restoreButtons('PO-3002')[0]);

    const toast = await screen.findByText(/Purchase order PO-3002 restored/);
    // Names the status AND the consequence, so nobody goes hunting the active list.
    expect(toast).toHaveTextContent(/cancelled/i);
    expect(toast).toHaveTextContent(/stays off the active list/i);
    // `warning` renders with role="alert" (like error, unlike success) so a screen reader
    // interrupts rather than queueing the one message the user has to act on.
    expect(toast.closest('[role="alert"]')).not.toBeNull();
  });

  test('a SENT PO restores with a plain success toast — no warning where none is due', async () => {
    mockedApi.restorePurchaseOrder.mockImplementationOnce(async () => {
      deletedBook = [deletedPurchaseOrders[1]];
      return { message: 'Purchase order restored' };
    });
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedView();

    fireEvent.click(restoreButtons('PO-3001')[0]);

    const toast = await screen.findByText('Purchase order PO-3001 restored');
    expect(toast).toBeInTheDocument();
    expect(toast).not.toHaveTextContent(/stays off the active list/i);
    // A SENT PO genuinely does return to the active book, so this must NOT be an alert.
    expect(toast.closest('[role="alert"]')).toBeNull();
  });
});

/**
 * The Deleted view is re-read on every entry — that rule has to survive leaving the tab.
 */
describe('Purchasing — the Deleted view does not persist across tabs', () => {
  test('a tab round-trip lands back on the Active book, not a stale archive', async () => {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedView();
    expect(deletedReads()).toHaveLength(1);

    // Leave for Vendors and come back to Orders.
    fireEvent.click(screen.getByRole('button', { name: /vendors/i }));
    await screen.findByText('VND-005');
    fireEvent.click(screen.getByRole('button', { name: /purchase orders/i }));

    // The ACTIVE book is what renders. Left as-is, poView would still be 'deleted' and the
    // archive would re-appear with rows nobody re-fetched — the one thing showPOView's
    // fetch-on-every-entry rule exists to prevent.
    await screen.findAllByText('PO-2001');
    expect(screen.getByRole('heading', { name: 'Purchase Orders' })).toBeInTheDocument();
    expect(deletedReads()).toHaveLength(1);
  });
});

/**
 * RBAC — the VERB is gated, the VIEW is not.
 *
 * Mirrors the server: POST .../{id}/restore is require_role([ADMIN, MANAGER]), while the
 * list endpoint stays on get_current_user because `deleted_only` returns rows the same
 * reader could already see before the delete. So a supervisor can open the archive and
 * read it — they just have no button. Each negative asserts the deleted row itself is on
 * screen, so a page that rendered nothing cannot pass as correct gating.
 */
describe('Purchasing — restore RBAC gating', () => {
  it.each(['admin', 'manager'])('offers Restore on every deleted row for %s', async (role) => {
    mockAuthUser = { id: 1, role };
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedView();

    expect(restoreButtons('PO-3001').length).toBeGreaterThan(0);
    expect(restoreButtons('PO-3002').length).toBeGreaterThan(0);
    expect(screen.getByText(/These purchase orders are deleted records/i)).toHaveTextContent(
      /Restore one to put it back in the active book/i,
    );
  });

  it.each(['supervisor', 'quality', 'operator'])(
    'lets %s READ the deleted view but withholds every Restore control',
    async (role) => {
      mockAuthUser = { id: 9, role };
      renderPurchasing();
      await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
      await openDeletedView();

      // The view opened and the rows are there — the negatives below are about the
      // button, not about a blank page.
      expect(screen.getByRole('heading', { name: 'Deleted Purchase Orders' })).toBeInTheDocument();
      expect(screen.getAllByText('PO-3001').length).toBeGreaterThan(0);
      expect(screen.getAllByText('PO-3002').length).toBeGreaterThan(0);
      expect(deletedReads()).toHaveLength(1);

      expect(allRestoreButtons()).toHaveLength(0);
      expect(mockedApi.restorePurchaseOrder).not.toHaveBeenCalled();
      // ...and the banner tells them who can, instead of silently offering nothing.
      expect(screen.getByText(/These purchase orders are deleted records/i)).toHaveTextContent(
        /An admin or manager can restore one/i,
      );
    },
  );
});
