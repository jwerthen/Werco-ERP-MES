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
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react';
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
    restoreVendor: jest.fn(),
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

/**
 * The vendor list endpoint's row shape.
 *
 * Same tri-state provenance block as PORow below, plus one field the PO twin has no
 * analogue for: `is_active_before_delete`. Restoring a vendor PRESERVES the `is_active`
 * it had when it was deleted (an approved-supplier list is an AS9100D-controlled
 * artifact — a supplier the shop deliberately switched off must not come back looking
 * selectable just because somebody undid a delete), so this is the ONLY field that can
 * answer "what do I get if I click Restore?" before the click. The row's own `is_active`
 * cannot: the delete forces it false on every deleted row, so it is uninformative by
 * construction.
 *
 * `undefined` is a real, currently-live fourth state — see the "does not report" test.
 */
type VendorRow = {
  id: number;
  code: string;
  name: string;
  contact_name?: string;
  email?: string;
  is_approved: boolean;
  is_active?: boolean;
  version?: number;
  is_deleted?: boolean;
  deleted_at?: string;
  deleted_by_name?: string | null;
  is_active_before_delete?: boolean | null;
};

const vendors: VendorRow[] = [
  { id: 5, code: 'VND-005', name: 'Acme Aerospace', is_approved: true, is_active: true, version: 0 },
];

// Straddles the UTC/Central boundary in the same deliberate way the PO fixtures do:
// 03:15Z on Aug 15 is 10:15 PM on Aug 14 in Chicago, so a cell that formatted the raw
// instant lands on the wrong DAY and the assertion catches it.
const VENDOR_DELETED_AT_901 = '2026-08-15T03:15:00Z';
const VENDOR_DELETED_AT_902 = '2026-08-10T16:45:00Z';

/**
 * The deleted vendor book.
 *
 * The two rows are the two halves of the owner's decision, not filler:
 *
 *  - VND-901 was DEACTIVATED before it was deleted (`is_active_before_delete: false`).
 *    Restoring it hands back a supplier that is still switched off. This is the row every
 *    "the decision is visible" assertion below is aimed at.
 *  - VND-902 was active (`true`), so a restore genuinely returns it to the vendor list.
 *
 * VND-902 also carries `deleted_by_name: null` — the departed-employee case, which the
 * server can legitimately answer (SoftDeleteMixin.deleted_by is a bare Integer with no FK,
 * so the name is resolved from `users` and that row can be gone).
 *
 * Both rows are `is_active: false`, because the delete forces that. It is exactly why the
 * deleted view must NOT AND in the endpoint's `active_only` default — that pairing returns
 * an empty archive forever — and why the row's own `is_active` is useless as a signal.
 */
const deletedVendors: VendorRow[] = [
  {
    id: 91,
    code: 'VND-901',
    name: 'Rusted Fastener Co',
    contact_name: 'Pat Nguyen',
    email: 'pat@rustedfastener.example',
    is_approved: true,
    is_active: false,
    is_deleted: true,
    deleted_at: VENDOR_DELETED_AT_901,
    deleted_by_name: 'Dana Ruiz',
    is_active_before_delete: false,
  },
  {
    id: 92,
    code: 'VND-902',
    name: 'Beta Alloys Supply',
    contact_name: 'Sam Ortiz',
    email: 'sam@betaalloys.example',
    is_approved: false,
    is_active: false,
    is_deleted: true,
    deleted_at: VENDOR_DELETED_AT_902,
    deleted_by_name: null,
    is_active_before_delete: true,
  },
];

// The same book with the prior-state field stripped — i.e. what a server that does not
// send `is_active_before_delete` returns. Not hypothetical: see the dedicated test.
const deletedVendorsWithoutRestoreState: VendorRow[] = deletedVendors.map(
  ({ is_active_before_delete: _omitted, ...rest }) => rest,
);

// Stand-ins for the server's two vendor books, so a restore can actually change what the
// next read returns. Reassigned per-test; read by the getVendors mock.
let activeVendorBook: VendorRow[] = [];
let deletedVendorBook: VendorRow[] = [];
// Live-but-DEACTIVATED vendors. A third book because the server has three answers, not
// two: `deleted_only=true` -> the archive, `active_only=false` -> every live row
// (active + inactive), and the bare call -> the active rows only. Empty by default so
// every pre-existing test sees exactly the traffic it saw before.
let inactiveVendorBook: VendorRow[] = [];

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

// ---------------------------------------------------------------------------
// Vendor-side twins of the helpers above. Same shapes on purpose: the two archives are
// one feature, and a helper that drifted would be the first sign the UI had.
// ---------------------------------------------------------------------------

// The Vendors tab's Active/Deleted switch. Scoping to role="group" is REQUIRED for the
// same reason as the PO switch: once the archive renders, DataTable's sortable "Deleted"
// column header is a second <button> with that exact accessible name.
const vendorViewSwitch = () => within(screen.getByRole('group', { name: 'Vendor view' }));
const vendorViewButton = (label: 'Active' | 'Inactive' | 'Deleted') =>
  vendorViewSwitch().getByRole('button', { name: label });

// DataTable renders the desktop table AND the mobile cards in jsdom, so every row-level
// control exists twice. Plural queries, always.
const restoreVendorButtons = (vendorName: string) =>
  screen.queryAllByRole('button', { name: `Restore vendor ${vendorName}` });
const allRestoreVendorButtons = () => screen.queryAllByRole('button', { name: /^Restore vendor/ });

/**
 * Every "Inactive" the ROWS render — i.e. the "Restores As" badges — excluding the
 * segmented view control, whose third button is now also labelled "Inactive". Scoping is
 * required, not tidiness: the same trap the "Active" assertions below already document.
 */
const restoresAsInactiveBadges = () =>
  screen.queryAllByText('Inactive').filter((el) => el.tagName !== 'BUTTON');

/** The desktop <tr> for a vendor, found by its code cell. */
const vendorRowFor = (code: string): HTMLTableRowElement => {
  const row = screen
    .getAllByText(code)
    .map((el) => el.closest('tr'))
    .find((tr): tr is HTMLTableRowElement => tr !== null);
  if (!row) throw new Error(`No table row rendered for ${code}`);
  return row;
};

/** Switch to the Vendors tab and wait for the live book. */
const openVendorsTab = async () => {
  fireEvent.click(screen.getByRole('button', { name: /vendors/i }));
  await screen.findByText('VND-005');
};

/** Switch to the Vendors tab, then into its Deleted view, and wait for its rows. */
const openDeletedVendorView = async () => {
  await openVendorsTab();
  fireEvent.click(vendorViewButton('Deleted'));
  await screen.findAllByText('VND-901');
};

/** Every getVendors call that asked for the deleted book. */
const deletedVendorReads = () =>
  mockedApi.getVendors.mock.calls.filter(([params]) => params?.deleted_only === true);
/** ...and every call that did not — i.e. the live vendor list. */
const activeVendorReadCount = () =>
  mockedApi.getVendors.mock.calls.length - deletedVendorReads().length;

beforeEach(() => {
  jest.clearAllMocks();
  mockAuthUser = { id: 1, role: 'manager' };
  deletedBook = deletedPurchaseOrders;
  activeVendorBook = vendors;
  deletedVendorBook = deletedVendors;
  inactiveVendorBook = [];
  // Answers as the endpoint does: ONE route serves both vendor books and `deleted_only` is
  // the only thing that tells them apart. Note what this mock deliberately does NOT model —
  // `active_only`. Server-side it defaults to TRUE and the delete forces `is_active = false`,
  // so an implementation that sent both would get an empty archive from the real API; the
  // "sends deleted_only ALONE" assertion below is what actually guards that, not this mock.
  mockedApi.getVendors.mockImplementation(async (params) => {
    if (params?.deleted_only) return deletedVendorBook;
    // `active_only: false` is the ONLY way to see a live-but-deactivated vendor: the
    // param defaults to TRUE server-side, so the bare call cannot return one. Modelled
    // faithfully, because the Inactive view's whole correctness rests on sending it.
    if (params?.active_only === false) return [...activeVendorBook, ...inactiveVendorBook];
    return activeVendorBook;
  });
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

/* ==========================================================================================
 * VENDORS — the same feature, the same shape.
 *
 * The vendor restore VERB (POST /purchasing/vendors/{id}/restore) and its client wrapper
 * `api.restoreVendor` both predate this UI, and the wrapper had ZERO call sites. What was
 * missing, exactly as for POs, was DISCOVERY: every read of GET /purchasing/vendors
 * hard-filtered `is_deleted == False`, so a soft-deleted vendor was invisible to every
 * caller and there was nothing a Restore button could act on. That gap turned sharp when
 * vendor reads were tightened so a soft-deleted vendor is no longer resolvable on five
 * write paths — refusals that shipped with no way to undo the delete from the UI.
 *
 * Two things are vendor-specific and get proportionally more attention below.
 *
 * 1. THE `active_only` TRAP. `list_vendors` carries `active_only: bool = True` as its
 *    DEFAULT, and `delete_vendor` sets `is_active = False` on the way out. ANDed, that
 *    returns an EMPTY LIST always — the restore view would read "no deleted vendors" no
 *    matter how many exist. The server carves `active_only` out of the deleted view; the
 *    client's half of the bargain is to send `deleted_only` ALONE, which is asserted below
 *    with an exact-argument match rather than a loose "was called with something".
 *
 * 2. RESTORE PRESERVES `is_active` — an explicit owner decision, and a compliance one. An
 *    approved-supplier list is an AS9100D-controlled artifact: a supplier the shop
 *    deliberately DEACTIVATED and then deleted must not come back looking active and
 *    selectable merely because somebody undid the delete. Undoing a delete restores a
 *    RECORD; it is not an approval decision and must not silently make one. That decision
 *    is invisible unless the UI says so, so it is pinned in BOTH of its moments — before
 *    the click (the banner, and the "Restores As" column when the server reports it) and
 *    after (the `warning` toast). These are the tests that stop the signal being quietly
 *    dropped in a later refactor.
 * ========================================================================================== */

describe('Purchasing — the deleted-vendor view', () => {
  test('the default page load does NOT send deleted_only — the live vendor list is untouched', async () => {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

    // Called with NO arguments at all, exactly as before the param existed. Asserted as
    // zero-args rather than `{ deleted_only: false }` on purpose: axios omits undefined
    // keys, so an absent key is what keeps the query string byte-identical.
    expect(mockedApi.getVendors).toHaveBeenCalledWith();
    expect(deletedVendorReads()).toHaveLength(0);

    await openVendorsTab();
    // The live book is what rendered, and no deleted vendor leaked into it.
    expect(screen.getAllByText('VND-005').length).toBeGreaterThan(0);
    expect(screen.queryAllByText('VND-901')).toHaveLength(0);
    expect(deletedVendorReads()).toHaveLength(0);
  });

  test('switching to Deleted fetches with deleted_only ALONE and shows only the deleted book', async () => {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

    await openDeletedVendorView();

    // EXACT argument match, and this is the load-bearing assertion of the vendor half.
    // `{ deleted_only: true, active_only: false }` would also "work" against the real
    // server, but `{ deleted_only: true, active_only: true }` — the shape you get by
    // reusing the live list's params — returns an empty archive forever, because the
    // delete forces is_active false. Sending the key alone is the only shape that cannot
    // be wrong.
    expect(mockedApi.getVendors).toHaveBeenCalledWith({ deleted_only: true });
    expect(deletedVendorReads()).toHaveLength(1);
    expect(deletedVendorReads()[0][0]).toEqual({ deleted_only: true });

    // Both deleted vendors are here, and the live one is NOT — two separate books, never
    // merged into one table where a dead supplier could be put on a purchase order.
    expect(screen.getAllByText('VND-901').length).toBeGreaterThan(0);
    expect(screen.getAllByText('VND-902').length).toBeGreaterThan(0);
    expect(screen.queryAllByText('VND-005')).toHaveLength(0);
  });

  // Every entry re-reads, not just the first: somebody else may have deleted or restored
  // in the meantime, and a stale archive is how you click Restore on a row the server
  // will refuse.
  test('re-fetches the deleted vendor book on every entry into the view', async () => {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

    await openDeletedVendorView();
    expect(deletedVendorReads()).toHaveLength(1);

    fireEvent.click(vendorViewButton('Active'));
    await screen.findByText('VND-005');
    fireEvent.click(vendorViewButton('Deleted'));
    await screen.findAllByText('VND-901');

    expect(deletedVendorReads()).toHaveLength(2);
  });

  test('a tab round-trip lands back on the Active vendor book, not a stale archive', async () => {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedVendorView();
    expect(deletedVendorReads()).toHaveLength(1);

    // Leave for Orders and come back to Vendors.
    fireEvent.click(screen.getByRole('button', { name: /purchase orders/i }));
    await screen.findAllByText('PO-2001');
    fireEvent.click(screen.getByRole('button', { name: /vendors/i }));

    await screen.findByText('VND-005');
    expect(screen.getByRole('heading', { name: 'Vendors' })).toBeInTheDocument();
    expect(deletedVendorReads()).toHaveLength(1);
  });
});

describe('Purchasing — deleted vendors are distinguishable from live ones', () => {
  test('the deleted view is labelled, banner-marked, dimmed, and states the two books apart', async () => {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedVendorView();

    // Heading flips, and the switch reports which book you are in to assistive tech.
    expect(screen.getByRole('heading', { name: 'Deleted Vendors' })).toBeInTheDocument();
    expect(vendorViewButton('Deleted')).toHaveAttribute('aria-pressed', 'true');
    expect(vendorViewButton('Active')).toHaveAttribute('aria-pressed', 'false');

    // A notice strip says what these rows ARE — records, off the vendor list, unusable on
    // a purchase order.
    const banner = screen.getByText(/These vendors are deleted records/i);
    expect(banner).toHaveTextContent(/cannot be selected on a purchase order/i);

    // Rows are visually dimmed, not merely differently laid out.
    expect(vendorRowFor('VND-901').className).toMatch(/opacity-70/);

    // Provenance columns exist only here.
    expect(screen.getByRole('columnheader', { name: /Deleted By/i })).toBeInTheDocument();
  });

  // The dangerous direction. The active vendor table's row actions are Edit and Delete;
  // a deleted vendor is a record, and the server agrees — PUT will not even resolve one.
  // Restore is the only affordance the archive offers.
  test('deleted vendor rows carry NO Edit or Delete — only Restore', async () => {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedVendorView();

    expect(screen.queryAllByRole('button', { name: 'Edit' })).toHaveLength(0);
    expect(screen.queryAllByRole('button', { name: 'Delete' })).toHaveLength(0);
    // ...and the row is genuinely on screen, so the absences above mean something.
    expect(screen.getAllByText('VND-901').length).toBeGreaterThan(0);
    expect(restoreVendorButtons('Rusted Fastener Co').length).toBeGreaterThan(0);
  });

  // The mirror image: nothing on the LIVE book offers a restore, which would be a control
  // that can only ever 400 (the endpoint refuses a vendor that is not deleted).
  test('the live vendor view shows no restore control', async () => {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openVendorsTab();

    expect(screen.getAllByText('VND-005').length).toBeGreaterThan(0);
    // Manager, so the live row DOES carry its normal write controls — the absence below
    // is about Restore specifically, not about an empty or unauthorized table.
    expect(screen.getAllByRole('button', { name: 'Delete' }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole('button', { name: 'Edit' }).length).toBeGreaterThan(0);
    expect(allRestoreVendorButtons()).toHaveLength(0);
  });

  // `deleted_at` is UTC over the wire and MUST render in shop-local Central. The fixture
  // instant is 03:15Z on Aug 15, which is Aug 14 in Chicago — so a cell that formatted the
  // raw instant, or leaned on the viewer's timezone, lands on the wrong DAY.
  //
  // BOTH renderers are pinned, and that is not belt-and-braces: DataTable emits the desktop
  // table AND the mobile cards, so a `toLocaleString()` in one is invisible to any assertion
  // the other can satisfy.
  test('renders deleted_at through the Central-time helper in BOTH renderers, never the raw ISO', async () => {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedVendorView();

    const central = formatCentralDateTime(VENDOR_DELETED_AT_901);
    // Guard on the fixture itself: if this ever stops straddling the date boundary the
    // assertions below quietly stop proving anything.
    expect(central).toMatch(/Aug 14/);

    // The desktop cell, named explicitly so a failure says WHICH renderer drifted.
    expect(within(vendorRowFor('VND-901')).getByText(central)).toBeInTheDocument();
    // ...and exactly the two elements DataTable renders per row carry it.
    expect(screen.getAllByText(central)).toHaveLength(2);

    // The raw wire value never reaches the screen, and neither does the UTC calendar day
    // nor a locale-default `toLocaleString()` shape.
    expect(screen.queryAllByText(VENDOR_DELETED_AT_901)).toHaveLength(0);
    expect(screen.queryAllByText(/Aug 15, 2026/)).toHaveLength(0);
    expect(screen.queryAllByText(/^\d{1,2}\/\d{1,2}\/\d{4}/)).toHaveLength(0);

    // The departed-user case: deleted_by_name comes back null and the cell says so rather
    // than rendering blank.
    expect(within(vendorRowFor('VND-901')).getByText('Dana Ruiz')).toBeInTheDocument();
    expect(within(vendorRowFor('VND-902')).getByText('Unknown')).toBeInTheDocument();
  });
});

/**
 * THE OWNER'S DECISION, MADE VISIBLE BEFORE THE CLICK.
 *
 * "Restore should keep the old is_active state." A vendor that was switched off and then
 * deleted comes back switched off — which is the RIGHT behavior and the SURPRISING one, so
 * the screen has to say it before somebody clicks Restore expecting a usable supplier.
 *
 * Nothing else on the row can carry that signal: `is_active` is forced false on every
 * deleted row by the delete itself, so it is uninformative by construction. Only the
 * server-reported prior state can answer it, and the UI's contract for it is deliberately
 * three-valued — see the second test, which pins the case where the server says nothing.
 */
describe('Purchasing — a vendor that will restore INACTIVE says so before you click', () => {
  test('the archive flags the pre-delete state per row, in BOTH renderers', async () => {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedVendorView();

    // The column exists at all — the header is what makes the badges below legible rather
    // than a bare word in a cell.
    expect(screen.getByRole('columnheader', { name: /Restores As/i })).toBeInTheDocument();

    // VND-901 was deactivated before deletion, so the row says so, in amber, BEFORE any
    // click. This is the assertion the whole feature turns on.
    const inactiveCell = within(vendorRowFor('VND-901')).getByText('Inactive');
    expect(inactiveCell).toBeInTheDocument();
    expect(inactiveCell.className).toMatch(/amber/);

    // Desktop table + mobile card. A signal present in only one renderer is a signal half
    // the users never see.
    expect(restoresAsInactiveBadges()).toHaveLength(2);

    // ...and VND-902, which was active, is NOT flagged — a warning on every row is a
    // warning on none. Scoped to the row because "Active" is also the label of the
    // segmented view control.
    expect(within(vendorRowFor('VND-902')).getByText('Active')).toBeInTheDocument();
    expect(within(vendorRowFor('VND-902')).queryByText('Inactive')).toBeNull();

    // The banner states the rule unconditionally, so the guarantee is legible even to
    // someone who does not parse the column.
    expect(screen.getByText(/These vendors are deleted records/i)).toHaveTextContent(
      /a supplier that was switched off comes back switched off/i,
    );
  });

  /**
   * The third state. `VendorResponse` DOES ship `is_active_before_delete` now, so this is
   * no longer the default production answer — but it is still reachable, and not rarely:
   * the SPA and the API deploy separately (Vercel / Railway), so this page runs against a
   * backend one deploy behind it on every release.
   *
   * The UI's answer is to HIDE the column rather than default it to "Active". That is the
   * safety property: a screen that says Active while the server hands back a deactivated
   * supplier is the precise failure this feature exists to prevent, and a confidently
   * wrong label is worse than no label. The banner still states the rule, so the
   * guarantee survives the column going dark.
   */
  test('hides the column entirely when the server does not report the prior state', async () => {
    deletedVendorBook = deletedVendorsWithoutRestoreState;
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedVendorView();

    expect(screen.queryAllByRole('columnheader', { name: /Restores As/i })).toHaveLength(0);
    // Not defaulted to a claim we cannot support, in either direction.
    expect(restoresAsInactiveBadges()).toHaveLength(0);
    expect(within(vendorRowFor('VND-901')).queryByText('Active')).toBeNull();

    // The rows themselves still render, so the absences above are about the column and not
    // about a table that failed to draw.
    expect(screen.getAllByText('VND-901').length).toBeGreaterThan(0);
    expect(screen.getAllByText('VND-902').length).toBeGreaterThan(0);
    expect(restoreVendorButtons('Rusted Fastener Co').length).toBeGreaterThan(0);

    // ...and the rule is still stated, which is what keeps this degradation honest rather
    // than silent.
    expect(screen.getByText(/These vendors are deleted records/i)).toHaveTextContent(
      /a supplier that was switched off comes back switched off/i,
    );
  });
});

/**
 * THE OTHER HALF OF THE RESTORE STORY, and the reason it is not optional.
 *
 * Restore preserves the pre-delete `is_active`, and a vendor deleted before migration 082
 * has no recorded prior state, so it comes back INACTIVE. That is 100% of the vendors
 * already soft-deleted in production. Before this view existed, such a vendor was on NO
 * screen in the app: the Vendors list is `active_only` server-side, the Deleted view no
 * longer holds it, global search filters `is_active`, and the PO picker offers only
 * approved-and-active rows. The reactivation that the restore semantics explicitly depend
 * on — "a human must deliberately reactivate it" — was unperformable.
 *
 * So the assertions here are about REACHABILITY, not chrome: the row is listed, and the
 * control on it is the ordinary audited edit form (which carries the `is_active`
 * checkbox), not a new one-click "reactivate" verb that would read as undo.
 */
describe('Purchasing — the inactive-vendor view', () => {
  const inactiveVendors: VendorRow[] = [
    {
      id: 77,
      code: 'VND-077',
      name: 'Dormant Plating Inc',
      contact_name: 'Lee Park',
      email: 'lee@dormantplating.example',
      is_approved: true,
      is_active: false,
      version: 0,
    },
  ];

  const openInactiveVendorView = async () => {
    await openVendorsTab();
    fireEvent.click(vendorViewButton('Inactive'));
    await screen.findAllByText('VND-077');
  };

  test('sends active_only=false — the bare call cannot return a deactivated vendor', async () => {
    inactiveVendorBook = inactiveVendors;
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

    // The default page load still sends NOTHING. A leaked param here would change what
    // the live vendor list returns, which is the one thing this must not do.
    expect(mockedApi.getVendors).toHaveBeenCalledWith();

    await openInactiveVendorView();
    expect(mockedApi.getVendors).toHaveBeenCalledWith({ active_only: false });
    // ...and never paired with deleted_only: these are LIVE records, not the archive.
    expect(
      mockedApi.getVendors.mock.calls.some(
        ([params]) => params?.active_only === false && params?.deleted_only,
      ),
    ).toBe(false);
  });

  test('lists the deactivated vendor and keeps it off the active list', async () => {
    inactiveVendorBook = inactiveVendors;
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openVendorsTab();

    // Not on the selectable list...
    expect(screen.queryAllByText('VND-077')).toHaveLength(0);
    expect(screen.getAllByText('VND-005').length).toBeGreaterThan(0);

    // ...and present, with its identity intact, one click away.
    fireEvent.click(vendorViewButton('Inactive'));
    await screen.findAllByText('VND-077');
    expect(screen.getAllByText('Dormant Plating Inc').length).toBeGreaterThan(0);

    // The active vendor is NOT dragged into this table by the wider read. The two books
    // stay separate — a merged one is how a switched-off supplier gets ordered from.
    expect(screen.queryAllByText('VND-005')).toHaveLength(0);
  });

  test('the row action is the audited edit form, and it opens with is_active unticked', async () => {
    inactiveVendorBook = inactiveVendors;
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openInactiveVendorView();

    // Deliberately NOT a "Reactivate" button: reactivating an approved supplier is an
    // ordinary, separately audited PUT, and a one-click verb beside a Restore-shaped
    // table would be clicked as undo.
    expect(screen.queryAllByRole('button', { name: /reactivate/i })).toHaveLength(0);

    fireEvent.click(screen.getAllByRole('button', { name: 'Edit vendor Dormant Plating Inc' })[0]);

    const dialog = await screen.findByRole('dialog');
    const activeCheckbox = within(dialog).getByRole('checkbox', { name: /active/i });
    // Unticked, because that is this vendor's real state. Ticking it and saving is the
    // deliberate reactivation the restore semantics rest on.
    expect(activeCheckbox).not.toBeChecked();
  });

  test('explains the state, and points at it from the deleted view', async () => {
    inactiveVendorBook = inactiveVendors;
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

    // The archive tells the operator where a restored-inactive vendor will end up —
    // otherwise the restore succeeds into a screen they have no reason to open.
    await openDeletedVendorView();
    expect(screen.getByText(/These vendors are deleted records/i)).toHaveTextContent(
      /appears under Inactive, where it can be reactivated/i,
    );

    fireEvent.click(vendorViewButton('Inactive'));
    await screen.findAllByText('VND-077');
    expect(screen.getByText(/These vendors exist but are switched off/i)).toHaveTextContent(
      /tick Active to bring it back into use/i,
    );
  });

  test('an empty book renders the empty state, not a blank panel', async () => {
    inactiveVendorBook = [];
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openVendorsTab();
    fireEvent.click(vendorViewButton('Inactive'));

    // DataTable draws the desktop table AND the mobile cards in jsdom, so the empty state
    // renders twice. Plural query, as everywhere else in this file.
    expect((await screen.findAllByText('No inactive vendors')).length).toBeGreaterThan(0);
  });
});

describe('Purchasing — restoring a deleted vendor', () => {
  test("calls api.restoreVendor with THAT row's id, toasts, and re-reads both books", async () => {
    // The server-side effect of a successful restore of an ACTIVE-before-delete vendor:
    // it leaves the deleted book and reappears on the live one.
    mockedApi.restoreVendor.mockImplementationOnce(async () => {
      deletedVendorBook = [deletedVendors[0]];
      activeVendorBook = [...vendors, { ...deletedVendors[1], is_active: true, is_deleted: false }];
      return { message: 'Vendor restored' };
    });
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedVendorView();

    const activeReadsBefore = activeVendorReadCount();

    // Restore the SECOND row (default sort is deleted_at desc, so VND-901 is first): an id
    // taken from "the first row" would send 91 here.
    fireEvent.click(restoreVendorButtons('Beta Alloys Supply')[0]);

    await waitFor(() => expect(mockedApi.restoreVendor).toHaveBeenCalledWith(92));
    expect(mockedApi.restoreVendor).toHaveBeenCalledTimes(1);
    // It was active before the delete, so it genuinely returns to the vendor list — plain
    // success, no warning where none is due.
    expect(await screen.findByText('Vendor Beta Alloys Supply restored')).toBeInTheDocument();

    // BOTH books are re-read — the deleted one so the row leaves, the live one so it
    // reappears where it belongs.
    await waitFor(() => expect(deletedVendorReads()).toHaveLength(2));
    expect(activeVendorReadCount()).toBeGreaterThan(activeReadsBefore);

    // Wait for the table itself to come back (the refresh flips the page to its full-page
    // loading state briefly), THEN assert the row is gone — otherwise "absent" would also
    // be satisfied mid-refresh, when nothing at all is rendered.
    await screen.findAllByText('VND-901');
    expect(screen.queryAllByText('VND-902')).toHaveLength(0);
  });

  // Server-GATED: a 400 here is the normal case (somebody else already restored it). The
  // user needs the server's own sentence, and the row must not move.
  test('a refusal surfaces the verbatim detail and LEAVES the row in the deleted view', async () => {
    const detail = 'Vendor is not deleted';
    mockedApi.restoreVendor.mockRejectedValueOnce(http(400, detail));
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedVendorView();

    fireEvent.click(restoreVendorButtons('Rusted Fastener Co')[0]);

    await waitFor(() => expect(mockedApi.restoreVendor).toHaveBeenCalledWith(91));
    expect(await screen.findByText(detail)).toBeInTheDocument();

    // No success toast, and the row is exactly where it was — nothing moved on the
    // strength of a refusal.
    expect(screen.queryByText(/Rusted Fastener Co restored/)).toBeNull();
    expect(screen.getAllByText('VND-901').length).toBeGreaterThan(0);
    expect(restoreVendorButtons('Rusted Fastener Co').length).toBeGreaterThan(0);

    // ...but the archive IS re-read: a 4xx means the server disagrees with what this table
    // shows, so the list converges on the server's answer instead of keeping a phantom row
    // whose every click reproduces the same refusal. Still non-optimistic — the row can
    // only move because a fresh server response moved it.
    await waitFor(() => expect(deletedVendorReads()).toHaveLength(2));
  });

  test('falls back to a generic message when the server sends no detail, and does NOT re-read', async () => {
    mockedApi.restoreVendor.mockRejectedValueOnce(http(500));
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedVendorView();

    fireEvent.click(restoreVendorButtons('Rusted Fastener Co')[0]);

    expect(await screen.findByText('Failed to restore vendor')).toBeInTheDocument();
    expect(screen.getAllByText('VND-901').length).toBeGreaterThan(0);
    // The other half of the re-read rule: a 5xx / offline failure tells us nothing about
    // this row, and a re-read that also failed would swap the archive for an error state
    // over a transient blip. Only a 4xx — the server disagreeing — re-reads.
    expect(deletedVendorReads()).toHaveLength(1);
  });

  // House rule for server-gated actions. Nothing may move before the server answers, or
  // the UI shows a state the server might refuse.
  test('is NON-OPTIMISTIC: the row stays put until the server answers', async () => {
    let resolveRestore: (value: { message: string }) => void = () => undefined;
    mockedApi.restoreVendor.mockImplementationOnce(
      () =>
        new Promise<{ message: string }>((resolve) => {
          resolveRestore = resolve;
        }),
    );
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedVendorView();

    fireEvent.click(restoreVendorButtons('Rusted Fastener Co')[0]);
    await waitFor(() => expect(mockedApi.restoreVendor).toHaveBeenCalledWith(91));

    // In flight: the row has not moved, neither book has been re-read, and no toast of any
    // kind has fired.
    expect(screen.getAllByText('VND-901').length).toBeGreaterThan(0);
    expect(deletedVendorReads()).toHaveLength(1);
    expect(screen.queryByText(/Rusted Fastener Co restored/)).toBeNull();

    // The clicked row carries the spinner + its loading label; every OTHER Restore is
    // merely disabled, so a second restore cannot be fired underneath the first. Asserted
    // across ALL of each row's buttons, not just [0]: the desktop table and the mobile
    // cards both render one, and a twin that stayed enabled would be a live double-submit
    // an index-0 assertion cannot see.
    expect(restoreVendorButtons('Rusted Fastener Co')).toHaveLength(2);
    restoreVendorButtons('Rusted Fastener Co').forEach((btn) => {
      expect(btn).toBeDisabled();
      expect(btn).toHaveTextContent('Restoring…');
    });
    expect(restoreVendorButtons('Beta Alloys Supply')).toHaveLength(2);
    restoreVendorButtons('Beta Alloys Supply').forEach((btn) => {
      expect(btn).toBeDisabled();
      expect(btn).not.toHaveTextContent('Restoring…');
    });

    // Only the server's answer moves it.
    deletedVendorBook = [deletedVendors[1]];
    resolveRestore({ message: 'Vendor restored' });

    await waitFor(() => expect(deletedVendorReads()).toHaveLength(2));
    await screen.findAllByText('VND-902');
    expect(screen.queryAllByText('VND-901')).toHaveLength(0);
  });
});

/**
 * RESTORED, BUT WHERE DID IT GO? — the owner's decision, made visible AFTER the click.
 *
 * Restore preserves the pre-delete `is_active`, and the Vendors tab reads the live list
 * (`active_only` server-side), so a vendor that was switched off leaves the archive
 * WITHOUT appearing on the vendor list. The server is right on both counts; what would be
 * wrong is saying "restored" and leaving somebody to hunt for a supplier that is on no
 * list they can open — or worse, to assume it is selectable on a PO again. That is what
 * the `warning` variant is for.
 */
describe('Purchasing — restoring a vendor the live list will not show', () => {
  test('a vendor that was INACTIVE before deletion restores with a WARNING toast naming why', async () => {
    // Leaves the archive; does NOT join the live book, because restore put it back
    // inactive and the live list is active_only.
    mockedApi.restoreVendor.mockImplementationOnce(async () => {
      deletedVendorBook = [deletedVendors[1]];
      return { message: 'Vendor restored' };
    });
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedVendorView();

    fireEvent.click(restoreVendorButtons('Rusted Fastener Co')[0]);

    const toast = await screen.findByText(/Vendor Rusted Fastener Co restored/);
    // Names the OUTCOME, the REASON, and the CONSEQUENCE — the whole owner decision in one
    // sentence, so nobody reads "restored" as "usable".
    expect(toast).toHaveTextContent(/restored as INACTIVE/);
    expect(toast).toHaveTextContent(/the state it had when it was deleted/i);
    expect(toast).toHaveTextContent(/stays off the vendor list, and off purchase orders/i);
    // `warning` renders with role="alert" (like error, unlike success) so a screen reader
    // interrupts rather than queueing the one message the user has to act on.
    expect(toast.closest('[role="alert"]')).not.toBeNull();
  });

  test('a vendor that was ACTIVE before deletion gets a plain success toast — no warning where none is due', async () => {
    mockedApi.restoreVendor.mockImplementationOnce(async () => {
      deletedVendorBook = [deletedVendors[0]];
      activeVendorBook = [...vendors, { ...deletedVendors[1], is_active: true, is_deleted: false }];
      return { message: 'Vendor restored' };
    });
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedVendorView();

    fireEvent.click(restoreVendorButtons('Beta Alloys Supply')[0]);

    const toast = await screen.findByText('Vendor Beta Alloys Supply restored');
    expect(toast).toBeInTheDocument();
    expect(toast).not.toHaveTextContent(/INACTIVE/);
    // It genuinely does return to the vendor list, so this must NOT be an alert.
    expect(toast.closest('[role="alert"]')).toBeNull();
  });
});

/**
 * RBAC — the VERB is gated, the VIEW is not.
 *
 * Mirrors the server: POST .../vendors/{id}/restore is require_role([ADMIN, MANAGER]),
 * while GET /purchasing/vendors stays on get_current_user because `deleted_only` returns
 * rows the same reader could already see before the delete. So a supervisor can open the
 * archive and read it — they just have no button. Each negative asserts the deleted row
 * itself is on screen, so a page that rendered nothing cannot pass as correct gating.
 */
describe('Purchasing — vendor restore RBAC gating', () => {
  it.each(['admin', 'manager'])('offers Restore on every deleted vendor row for %s', async (role) => {
    mockAuthUser = { id: 1, role };
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedVendorView();

    expect(restoreVendorButtons('Rusted Fastener Co').length).toBeGreaterThan(0);
    expect(restoreVendorButtons('Beta Alloys Supply').length).toBeGreaterThan(0);
    expect(screen.getByText(/These vendors are deleted records/i)).toHaveTextContent(
      /Restore one to put it back in the vendor list/i,
    );
  });

  it.each(['supervisor', 'quality', 'operator'])(
    'lets %s READ the deleted vendor view but withholds every Restore control',
    async (role) => {
      mockAuthUser = { id: 9, role };
      renderPurchasing();
      await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
      await openDeletedVendorView();

      // The view opened and the rows are there — the negatives below are about the button,
      // not about a blank page.
      expect(screen.getByRole('heading', { name: 'Deleted Vendors' })).toBeInTheDocument();
      expect(screen.getAllByText('VND-901').length).toBeGreaterThan(0);
      expect(screen.getAllByText('VND-902').length).toBeGreaterThan(0);
      expect(deletedVendorReads()).toHaveLength(1);
      // ...and the pre-click signal is NOT role-gated either: a reader who cannot restore
      // still needs to know what a restore would do before asking someone who can.
      expect(screen.getByRole('columnheader', { name: /Restores As/i })).toBeInTheDocument();
      expect(within(vendorRowFor('VND-901')).getByText('Inactive')).toBeInTheDocument();

      expect(allRestoreVendorButtons()).toHaveLength(0);
      expect(mockedApi.restoreVendor).not.toHaveBeenCalled();
      // ...and the banner tells them who can, instead of silently offering nothing.
      expect(screen.getByText(/These vendors are deleted records/i)).toHaveTextContent(
        /An admin or manager can restore one/i,
      );
    },
  );
});

/* ==========================================================================================
 * THE LEGACY ROW — a vendor deleted BEFORE its prior state was ever recorded.
 *
 * This block exists because the tests above, thorough as they are, left the single most
 * load-bearing case in the owner's decision unpinned. Their fixtures cover `false`, `true`
 * and "field absent"; they never cover `null`.
 *
 * `null` is not an edge case, it is the MAJORITY case at ship time. Migration 082 added the
 * `is_active_before_delete` sidecar forward-only with no backfill, so EVERY vendor deleted
 * before it landed carries NULL, and the owner ruled explicitly that those restore INACTIVE
 * — a deliberate break from the old unconditional `is_active = True`. The server implements
 * it as COALESCE(is_active_before_delete, FALSE); the screen has to agree, because it is the
 * screen that sets the expectation before the click.
 *
 * Why it needed its own test rather than trusting the code: `restoresAs` is written as
 * `=== true ? 'active' : 'inactive'` where the natural-reading form is `=== false ?
 * 'inactive' : 'active'`. Those two differ ONLY on null — and the loose one maps null to
 * "Active", i.e. paints the reassuring label on the one row class the server GUARANTEES
 * comes back switched off. Verified against this suite: swapping the strict form for the
 * loose one leaves all 45 preceding tests green. The tri-state fixtures cannot see it, so
 * only the assertions below stand between that comment and a future "simplification".
 * ========================================================================================== */

/** VND-901 as a pre-082 row: deleted back when nothing recorded the prior state. */
const legacyDeletedVendors: VendorRow[] = [
  { ...deletedVendors[0], is_active_before_delete: null },
  deletedVendors[1],
];

describe('Purchasing — a vendor whose prior state was never recorded', () => {
  test('a NULL prior state is shown as Inactive, never Active — the owner ruling for legacy rows', async () => {
    deletedVendorBook = legacyDeletedVendors;
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedVendorView();

    // The server answered (null IS an answer — COALESCE resolves it to false), so the
    // column is reported and shown; this is not the "server said nothing" path.
    expect(screen.getByRole('columnheader', { name: /Restores As/i })).toBeInTheDocument();

    // THE assertion. Amber, like a vendor that was explicitly deactivated, because the
    // outcome is identical: it comes back switched off.
    const cell = within(vendorRowFor('VND-901')).getByText('Inactive');
    expect(cell.className).toMatch(/amber/);
    // Desktop table + mobile card. A warning present in one renderer is a warning half the
    // users never see.
    expect(restoresAsInactiveBadges()).toHaveLength(2);

    // ...and emphatically NOT the reassuring label. This is the assertion the loose form of
    // `restoresAs` fails and every other test in this file passes.
    expect(within(vendorRowFor('VND-901')).queryByText('Active')).toBeNull();

    // Nor is it quietly downgraded to "we don't know". VND-901 is approved, has a contact,
    // a deleted_at and a deleted_by, so no other cell in this row renders a bare dash — a
    // dash here could ONLY be the 'unreported' placeholder, and the server did report.
    expect(within(vendorRowFor('VND-901')).queryByText('-')).toBeNull();

    // The row that reported `true` is still Active, so the null handling did not simply
    // paint the whole column amber and call it safe.
    expect(within(vendorRowFor('VND-902')).getByText('Active')).toBeInTheDocument();
  });

  // The post-click half, for the same row class. A legacy vendor leaves the archive without
  // joining the vendor list, so it draws the warning variant — and its wording has to cover
  // the never-recorded case, not just the deliberately-deactivated one.
  test('restoring a legacy row warns, and the wording covers "never recorded"', async () => {
    deletedVendorBook = legacyDeletedVendors;
    mockedApi.restoreVendor.mockImplementationOnce(async () => {
      // Came back inactive, so it is off the archive AND off the active_only vendor list.
      deletedVendorBook = [legacyDeletedVendors[1]];
      return { message: 'Vendor restored' };
    });
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedVendorView();

    fireEvent.click(restoreVendorButtons('Rusted Fastener Co')[0]);

    await waitFor(() => expect(mockedApi.restoreVendor).toHaveBeenCalledWith(91));
    const toast = await screen.findByText(/Vendor Rusted Fastener Co restored/);
    expect(toast).toHaveTextContent(/restored as INACTIVE/);
    // The legacy case specifically: "the state it had when it was deleted" is a claim the
    // system CANNOT make about a pre-082 row, so the sentence has to admit the other branch
    // or it is telling a legacy user something false about their own record.
    expect(toast).toHaveTextContent(/inactive when that was never recorded/i);
    expect(toast.closest('[role="alert"]')).not.toBeNull();
  });

  // `restoreStateReported` is a `.some()` across rows — a whole-view switch, on the premise
  // that the field is either in the schema or it is not. Pinning the mixed book documents
  // what happens if that premise ever stops holding: the column appears, and the row the
  // server did not answer for makes NO claim in either direction. That is the safe
  // degradation, and the one worth catching a change to.
  test('a MIXED book shows the column, and makes no claim for the row it has no answer for', async () => {
    deletedVendorBook = [deletedVendors[0], deletedVendorsWithoutRestoreState[1]];
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedVendorView();

    expect(screen.getByRole('columnheader', { name: /Restores As/i })).toBeInTheDocument();
    expect(within(vendorRowFor('VND-901')).getByText('Inactive')).toBeInTheDocument();

    // The unreported row is labelled neither way. Asserted as a pair of absences rather than
    // a dash, because the dash is a rendering detail while "asserts nothing" is the property.
    expect(within(vendorRowFor('VND-902')).queryByText('Active')).toBeNull();
    expect(within(vendorRowFor('VND-902')).queryByText('Inactive')).toBeNull();

    // Both rows are on screen and restorable, so the absences above are about the claim and
    // not about a table that failed to draw a row.
    expect(restoreVendorButtons('Rusted Fastener Co').length).toBeGreaterThan(0);
    expect(restoreVendorButtons('Beta Alloys Supply').length).toBeGreaterThan(0);
  });
});

/**
 * THE ARCHIVE'S OWN FAILURE AND EMPTY STATES.
 *
 * The restore view is the ONLY way to see a soft-deleted vendor, so "the list did not load"
 * and "there is nothing to restore" are not interchangeable — the first is recoverable and
 * the second is an answer. A failed read that rendered as an empty archive would tell an
 * admin their deleted vendor is gone for good, which is the worst lie this screen can tell.
 */
describe('Purchasing — the deleted-vendor archive load states', () => {
  test('a failed archive read renders the retryable error state, not an empty archive', async () => {
    let failDeletedRead = true;
    mockedApi.getVendors.mockImplementation(async (params) => {
      if (params?.deleted_only) {
        if (failDeletedRead) throw http(500);
        return deletedVendorBook;
      }
      return activeVendorBook;
    });

    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openVendorsTab();
    fireEvent.click(vendorViewButton('Deleted'));

    // The shared <ErrorState>, which carries role="alert" so the failure is announced.
    const errorState = await screen.findByTestId('error-state');
    expect(errorState).toHaveAttribute('role', 'alert');
    // ...and NOT the empty state, which would read as "no deleted vendors exist".
    expect(screen.queryAllByText('No deleted vendors')).toHaveLength(0);

    // The banner still frames what this view is, so the error is scoped to the table.
    expect(screen.getByText(/These vendors are deleted records/i)).toBeInTheDocument();

    // Retry re-runs the fetch — the whole point of the primitive — and the rows arrive.
    failDeletedRead = false;
    fireEvent.click(within(errorState).getByRole('button', { name: 'Retry' }));

    await screen.findAllByText('VND-901');
    expect(screen.queryAllByTestId('error-state')).toHaveLength(0);
    expect(deletedVendorReads()).toHaveLength(2);
    expect(restoreVendorButtons('Rusted Fastener Co').length).toBeGreaterThan(0);
  });

  test('an empty archive says so, and offers nothing to restore', async () => {
    deletedVendorBook = [];
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openVendorsTab();
    fireEvent.click(vendorViewButton('Deleted'));

    // DataTable renders the empty state in both the desktop and mobile trees.
    expect((await screen.findAllByText('No deleted vendors')).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/appear here so they can be restored/i).length).toBeGreaterThan(0);
    expect(allRestoreVendorButtons()).toHaveLength(0);
    // A real answer, not a failed read.
    expect(screen.queryAllByTestId('error-state')).toHaveLength(0);
    expect(deletedVendorReads()).toHaveLength(1);
  });
});

/**
 * THE STALE-RESPONSE LATCH.
 *
 * Entering the Deleted view fires a read on EVERY entry, so an Active/Deleted toggle can
 * leave two reads in flight. Without the request-sequence ref in `loadDeletedVendors`, the
 * slower (older) response wins and paints an archive nobody asked for — and on this screen a
 * stale archive is not cosmetic: it is a Restore button attached to a row the server will
 * refuse, or worse, a row that was already restored and is now live.
 *
 * The latch is a documented, deliberate piece of the implementation and nothing above
 * exercised it, so a refactor that dropped it would stay green.
 */
describe('Purchasing — a stale archive response cannot paint the view', () => {
  test('an older in-flight read that lands last is discarded', async () => {
    const pendingDeletedReads: Array<(rows: VendorRow[]) => void> = [];
    mockedApi.getVendors.mockImplementation(async (params) => {
      if (params?.deleted_only) {
        return new Promise<VendorRow[]>((resolve) => {
          pendingDeletedReads.push(resolve);
        });
      }
      return activeVendorBook;
    });

    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openVendorsTab();

    // Read #1 — left hanging.
    fireEvent.click(vendorViewButton('Deleted'));
    await waitFor(() => expect(pendingDeletedReads).toHaveLength(1));

    // Bounce out and back in: read #2, also hanging.
    fireEvent.click(vendorViewButton('Active'));
    await screen.findByText('VND-005');
    fireEvent.click(vendorViewButton('Deleted'));
    await waitFor(() => expect(pendingDeletedReads).toHaveLength(2));

    // The NEWER read answers first, with a one-row book.
    await act(async () => {
      pendingDeletedReads[1]([deletedVendors[1]]);
    });
    await screen.findAllByText('VND-902');

    // ...and only then does the older one answer, with the full two-row book. It must be
    // dropped: VND-901 belongs to a request the user already navigated away from.
    await act(async () => {
      pendingDeletedReads[0](deletedVendors);
    });

    expect(screen.queryAllByText('VND-901')).toHaveLength(0);
    expect(screen.getAllByText('VND-902').length).toBeGreaterThan(0);
    // ...and the view is not stuck in its loading state either, which is the other way a
    // naive latch fails (the stale response clears a flag the live one owns).
    expect(restoreVendorButtons('Beta Alloys Supply').length).toBeGreaterThan(0);
  });
});

/**
 * THE HONEST FALLBACK.
 *
 * The warning toast is decided by a MEMBERSHIP TEST — did the restored vendor show up on the
 * reloaded live list? Deliberately, even though `is_active_before_delete` is now on the wire:
 * that field is what the restore WILL do, read before the click, while the toast reports what
 * the server actually DID. Reading the outcome off the reload keeps the two independent, so a
 * prediction that turned out wrong is caught rather than echoed back. That proxy needs the
 * reload to have succeeded. When it did not, `loadData()` returns null and the page
 * deliberately falls back to the plain success toast rather than inferring absence from a
 * failure: the restore genuinely DID succeed, and the page is already showing its own load
 * error.
 *
 * Pinned because the tempting "simplification" is `!freshVendors?.some(...)`, which collapses
 * null into "absent" and would fire a confident INACTIVE warning off a network blip.
 */
describe('Purchasing — restore when the follow-up reload fails', () => {
  test('falls back to the plain success toast instead of inferring INACTIVE from a failed read', async () => {
    let failActiveRead = false;
    mockedApi.getVendors.mockImplementation(async (params) => {
      if (params?.deleted_only) return deletedVendorBook;
      if (failActiveRead) throw http(500);
      return activeVendorBook;
    });
    mockedApi.restoreVendor.mockImplementationOnce(async () => {
      failActiveRead = true;
      deletedVendorBook = [deletedVendors[1]];
      return { message: 'Vendor restored' };
    });

    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    await openDeletedVendorView();

    // VND-901 is the row that WOULD draw the warning on a successful reload — it was
    // inactive before deletion — so this is the strongest possible version of the case.
    fireEvent.click(restoreVendorButtons('Rusted Fastener Co')[0]);

    await waitFor(() => expect(mockedApi.restoreVendor).toHaveBeenCalledWith(91));
    expect(await screen.findByText('Vendor Rusted Fastener Co restored')).toBeInTheDocument();
    // No warning invented out of a failed read...
    expect(screen.queryByText(/restored as INACTIVE/)).toBeNull();
    // ...and no error toast either: the restore itself succeeded and saying otherwise would
    // send someone to re-do an action that already landed.
    expect(screen.queryByText('Failed to restore vendor')).toBeNull();
  });
});
