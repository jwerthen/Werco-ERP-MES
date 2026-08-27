/**
 * WorkOrders — the Deleted tab (the soft-delete archive) and Restore.
 *
 * Invariant 3 says a deleted work order is HIDDEN, not destroyed. This tab is the
 * only screen in the app that can see one, and `POST /work-orders/{id}/restore` is
 * the only way back. The properties locked here are the ones a refactor flattens:
 *
 * 1. **The tab is gated, and a deep link FALLS BACK rather than 403s.**
 *    `deleted_only=true` and the restore verb are role-gated on the server to
 *    admin/manager (+superuser). An operator who lands on `?tab=deleted` — from a
 *    pasted link or a stale bookmark — must get the work-order list, not an empty
 *    archive and not a forbidden screen, and must emit no request the server would
 *    refuse.
 *
 * 2. **The archive read is LAZY.** It is deliberately kept out of `loadWorkOrders`
 *    and out of the 30s poll / focus / websocket refresh loop, so the orders tab
 *    emits exactly the requests it always did. A test that only proves the fetch
 *    happens on the deleted tab would pass just as well if it fired on mount for
 *    everybody.
 *
 * 3. **A deleted row offers Restore and NOTHING else.**
 *    `GET /work-orders/{id}` 404s on a soft-deleted row, so click-through reads as
 *    the app breaking; Release / Delete / Duplicate / Save-as-template are all
 *    verbs a deleted row cannot honor. The archive uses its own column array
 *    precisely because the shared builder emits two unconditional detail links —
 *    which is exactly the regression this file catches if someone "simplifies" it
 *    back onto `buildWorkOrderColumns`.
 *
 * 4. **Restore is server-GATED, therefore NON-optimistic**, and its PARTIAL result
 *    raises the house `warning` toast, never `success`. That is the compliance-
 *    relevant one: a restore that could not re-open a material tie leaves the job
 *    with no demand for that material — no shortage shows and stock is never
 *    deducted until a count disagrees — and a green "restored" toast hides it.
 *    The assertions are on the toast's ROLE and colour token, not just its words,
 *    because a refactor that collapses the branch into `success` would keep the
 *    same sentence.
 *
 * Mocks `services/api` (the wire shapes are pinned in the api unit tests) and
 * silences the realtime refresh loop so the `getWorkOrders` call counts are
 * deterministic. DataTable mounts the desktop table AND the mobile cards in jsdom
 * (CSS hides one per breakpoint; jsdom applies none), so row controls are
 * legitimately present twice — queries here are scoped to the desktop table via
 * its `data-table` testid, or use `getAllBy*`.
 */

import React from 'react';
import { render, screen, waitFor, within, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import api from '../services/api';
import WorkOrders from './WorkOrders';
import { ToastProvider } from '../components/ui/Toast';
import type { WorkOrderRestoreResponse, WorkOrderSummary } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrders: jest.fn(),
    deleteWorkOrder: jest.fn(),
    releaseWorkOrder: jest.fn(),
    restoreWorkOrder: jest.fn(),
    listWorkOrderTemplates: jest.fn(),
  },
}));

const mockUser = { current: { id: 1, role: 'admin' as string, is_superuser: false } };

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: mockUser.current,
    isAuthenticated: true,
    isLoading: false,
  }),
}));

jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: jest.fn() }));

jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

const liveWorkOrder: WorkOrderSummary = {
  id: 1,
  work_order_number: 'WO-1001',
  part_id: 10,
  work_order_type: 'production',
  part_number: 'PN-AAA',
  part_name: 'Bracket Assembly',
  part_type: 'manufactured',
  status: 'draft',
  priority: 2,
  quantity_ordered: 50,
  quantity_complete: 0,
  customer_name: 'Acme Aero',
  // The three provenance fields are deliberately ABSENT on a live row — the
  // tri-state is the contract (see WorkOrderSummary's docblock).
};

/**
 * A deleted row that is also COMPLETE. Both halves matter: the server drops its
 * default complete/closed/cancelled exclusion on this view (an archive that hides
 * finished jobs is empty exactly when someone needs it), and a terminal status is
 * the second way a restore can succeed without putting the job on any list.
 */
const deletedComplete: WorkOrderSummary = {
  id: 7,
  work_order_number: 'WO-20260801-007',
  part_id: 70,
  work_order_type: 'production',
  part_number: 'PN-DEAD',
  part_name: 'Weld Fixture',
  part_type: 'manufactured',
  status: 'complete',
  priority: 3,
  quantity_ordered: 12,
  quantity_complete: 12,
  customer_name: 'Beta Defense',
  is_deleted: true,
  // 14:30Z on Aug 25 is 9:30 AM Central — the hour AND the format prove the
  // Central conversion rather than a raw `toLocaleString`.
  deleted_at: '2026-08-25T14:30:00Z',
  deleted_by_name: 'Dana Reyes',
};

/** Deleted with the actor's user row gone — `deleted_by_name` comes back null. */
const deletedDraft: WorkOrderSummary = {
  id: 8,
  work_order_number: 'WO-20260802-008',
  part_id: 80,
  work_order_type: 'production',
  part_number: 'PN-GONE',
  part_name: 'Spacer',
  part_type: 'manufactured',
  status: 'draft',
  priority: 2,
  quantity_ordered: 4,
  quantity_complete: 0,
  customer_name: 'Acme Aero',
  is_deleted: true,
  // 02:15Z on Aug 26 is 9:15 PM Central on Aug 25 — the DATE shifts too.
  deleted_at: '2026-08-26T02:15:00Z',
  deleted_by_name: null,
};

/** Mutable so a restore can take a row out of the archive the refetch re-reads. */
let deletedRows: WorkOrderSummary[] = [];

function renderAt(url: string) {
  return render(
    <ToastProvider>
      <MemoryRouter initialEntries={[url]}>
        <WorkOrders />
      </MemoryRouter>
    </ToastProvider>
  );
}

const deletedFetchCalls = () =>
  mockedApi.getWorkOrders.mock.calls.filter((call) => call[0]?.deleted_only === true);

/** The desktop table, once it has painted. Mobile cards mount too; ignore them. */
const desktopTable = async (): Promise<HTMLElement> =>
  (await screen.findAllByTestId('data-table'))[0] as HTMLElement;

/** The archive's desktop row for a work-order number. */
async function archiveRow(workOrderNumber: string): Promise<HTMLElement> {
  const table = await desktopTable();
  const cell = await within(table).findByText(workOrderNumber);
  const row = cell.closest('tr');
  if (!row) throw new Error(`no <tr> for ${workOrderNumber}`);
  return row as HTMLElement;
}

/** Open the Restore confirm dialog for a row and return its confirm button. */
async function openRestoreDialog(workOrderNumber: string): Promise<HTMLElement> {
  const [restoreButton] = screen.getAllByRole('button', {
    name: `Restore work order ${workOrderNumber}`,
  });
  await userEvent.click(restoreButton);
  const dialog = await screen.findByRole('dialog');
  return within(dialog).getByRole('button', { name: 'Restore' });
}

beforeEach(() => {
  jest.clearAllMocks();
  mockUser.current = { id: 1, role: 'admin', is_superuser: false };
  deletedRows = [deletedComplete, deletedDraft];
  mockedApi.getWorkOrders.mockImplementation(async (params?: { deleted_only?: boolean }) =>
    params?.deleted_only ? deletedRows : [liveWorkOrder]
  );
  mockedApi.listWorkOrderTemplates.mockResolvedValue({ templates: [], total: 0 });
});

describe('WorkOrders: the Deleted tab is gated on the restore population', () => {
  it('hides the tab from an operator', async () => {
    // `deleted_only=true` and POST /restore are role-gated to admin/manager
    // (+superuser). Showing an operator the tab could only ever produce a 403.
    mockUser.current = { id: 2, role: 'operator', is_superuser: false };
    renderAt('/work-orders');

    expect(await screen.findAllByText('WO-1001')).not.toHaveLength(0);
    expect(screen.queryByRole('button', { name: 'Deleted' })).not.toBeInTheDocument();
  });

  it('falls back to the work-order list on an operator deep link, and asks the server for nothing', async () => {
    // The fallback is silent on purpose: a forbidden screen (or an empty archive)
    // teaches nothing, and the request behind it would be refused anyway.
    mockUser.current = { id: 2, role: 'operator', is_superuser: false };
    renderAt('/work-orders?tab=deleted');

    expect(await screen.findAllByText('WO-1001')).not.toHaveLength(0);
    expect(deletedFetchCalls()).toHaveLength(0);
    expect(screen.queryAllByText('WO-20260801-007')).toHaveLength(0);
    // Nothing from the archive leaked into the live list either.
    expect(screen.queryByRole('button', { name: /^Restore work order/ })).not.toBeInTheDocument();
  });

  it('renders the tab for an admin', async () => {
    renderAt('/work-orders');
    await screen.findAllByText('WO-1001');

    expect(screen.getByRole('button', { name: 'Deleted' })).toBeInTheDocument();
  });

  it('renders the tab for a manager', async () => {
    // The backend widened soft-delete to MANAGER as well as ADMIN; the tab has to
    // track that population, not just admins.
    mockUser.current = { id: 3, role: 'manager', is_superuser: false };
    renderAt('/work-orders');
    await screen.findAllByText('WO-1001');

    expect(screen.getByRole('button', { name: 'Deleted' })).toBeInTheDocument();
  });
});

describe('WorkOrders: the archive read is lazy', () => {
  it('issues NO deleted_only request while the user is on the orders tab', async () => {
    renderAt('/work-orders');
    await screen.findAllByText('WO-1001');

    // Every mount fetch has happened by now; none of them may be the archive read.
    expect(mockedApi.getWorkOrders).toHaveBeenCalled();
    expect(deletedFetchCalls()).toHaveLength(0);
    expect(mockedApi.getWorkOrders).not.toHaveBeenCalledWith(
      expect.objectContaining({ deleted_only: true })
    );
  });

  it('fetches with deleted_only when the tab is entered', async () => {
    renderAt('/work-orders');
    await screen.findAllByText('WO-1001');

    await userEvent.click(screen.getByRole('button', { name: 'Deleted' }));

    await archiveRow('WO-20260801-007');
    expect(mockedApi.getWorkOrders).toHaveBeenCalledWith({ deleted_only: true });
    expect(deletedFetchCalls()).toHaveLength(1);
  });

  it('paints a ?tab=deleted deep link without waiting on the work-order fetch', async () => {
    // Same trap the Templates tab has: `loading` is cleared ONLY by
    // `loadWorkOrders`'s finally, so a deleted branch below that gate would leave
    // this deep link on a work-order skeleton forever.
    mockedApi.getWorkOrders.mockImplementation(
      async (params?: { deleted_only?: boolean }) =>
        params?.deleted_only ? deletedRows : new Promise<WorkOrderSummary[]>(() => {})
    );

    renderAt('/work-orders?tab=deleted');

    expect(await archiveRow('WO-20260801-007')).toBeInTheDocument();
    // `current: 'page'` disambiguates the TAB from the archive's sortable
    // "Deleted" column header, and asserts the tab is the selected one.
    expect(screen.getByRole('button', { name: 'Deleted', current: 'page' })).toBeInTheDocument();
  });
});

describe('WorkOrders: what an archived row shows', () => {
  it('renders the deleted-at stamp in Central and the deleting actor', async () => {
    renderAt('/work-orders?tab=deleted');
    const row = await archiveRow('WO-20260801-007');

    // 2026-08-25T14:30:00Z -> 9:30 AM Central. A `toLocaleString` would render the
    // runner's zone (and 2:30 PM on a UTC box); a missing conversion would too.
    expect(row).toHaveTextContent('Aug 25, 2026');
    expect(row).toHaveTextContent('9:30 AM');
    expect(row).toHaveTextContent('Dana Reyes');
  });

  it('shifts the calendar DATE when Central and UTC disagree', async () => {
    renderAt('/work-orders?tab=deleted');
    const row = await archiveRow('WO-20260802-008');

    // 2026-08-26T02:15:00Z is still Aug 25 in the shop.
    expect(row).toHaveTextContent('Aug 25, 2026');
    expect(row).toHaveTextContent('9:15 PM');
  });

  it('renders a neutral dash — not "null", not a guess — when deleted_by_name is null', async () => {
    // The actor's user row can be gone; provenance survives them, the NAME does not.
    renderAt('/work-orders?tab=deleted');
    const row = await archiveRow('WO-20260802-008');

    const cells = within(row).getAllByRole('cell');
    // Columns: Work Order | Part | Status | Qty | Due Date | Deleted | Deleted By | Actions
    expect(cells[6]).toHaveTextContent('—');
    expect(row).not.toHaveTextContent(/null|undefined/);
  });

  it('lists a COMPLETE work order — the archive drops the terminal-status exclusion', async () => {
    // The default orders list excludes complete/closed/cancelled. If that exclusion
    // were applied here too, a finished-then-deleted job would be invisible with no
    // way to restore it.
    renderAt('/work-orders?tab=deleted');

    expect(await archiveRow('WO-20260801-007')).toHaveTextContent(/complete/i);
  });
});

describe('WorkOrders: an archived row offers Restore and nothing else', () => {
  it('offers no detail-page navigation', async () => {
    renderAt('/work-orders?tab=deleted');
    await archiveRow('WO-20260801-007');

    // GET /work-orders/{id} 404s on a soft-deleted row. The shared column builder
    // emits TWO unconditional detail links, which is why this view has its own.
    const detailLinks = Array.from(
      document.querySelectorAll<HTMLAnchorElement>('a[href^="/work-orders/"]')
    ).filter((a) => /^\/work-orders\/\d+$/.test(a.getAttribute('href') ?? ''));
    expect(detailLinks).toHaveLength(0);
    expect(screen.queryByRole('link', { name: 'WO-20260801-007' })).not.toBeInTheDocument();
  });

  it('offers no Release, Delete, Duplicate or Save-as-template control', async () => {
    renderAt('/work-orders?tab=deleted');
    await archiveRow('WO-20260801-007');

    expect(screen.queryByTitle('Release')).not.toBeInTheDocument();
    expect(screen.queryByTitle('Delete')).not.toBeInTheDocument();
    expect(screen.queryByTitle('Duplicate')).not.toBeInTheDocument();
    expect(screen.queryByTitle('Save as template')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Release WO-/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Delete WO-/ })).not.toBeInTheDocument();
    // ...and no inline due-date pencil: a deleted job is on no schedule.
    expect(screen.queryByRole('button', { name: /^Edit due date for/ })).not.toBeInTheDocument();
    // The one verb that IS offered.
    expect(
      screen.getAllByRole('button', { name: 'Restore work order WO-20260801-007' }).length
    ).toBeGreaterThan(0);
  });
});

describe('WorkOrders: restoring is confirmed, non-optimistic, and honest about partials', () => {
  it('confirms first, holds the row through the flight, and drops it only after the server answers', async () => {
    let resolveRestore: (value: WorkOrderRestoreResponse) => void = () => {};
    mockedApi.restoreWorkOrder.mockReturnValue(
      new Promise<WorkOrderRestoreResponse>((resolve) => {
        resolveRestore = resolve;
      })
    );

    renderAt('/work-orders?tab=deleted');
    await archiveRow('WO-20260802-008');

    const confirmButton = await openRestoreDialog('WO-20260802-008');
    // The row control only OPENS the dialog — nothing was sent yet.
    expect(mockedApi.restoreWorkOrder).not.toHaveBeenCalled();

    await userEvent.click(confirmButton);
    expect(mockedApi.restoreWorkOrder).toHaveBeenCalledWith(8);

    // NON-OPTIMISTIC: mid-flight the row is still in the archive, the dialog is
    // still up in its pending state, and a second click is inert.
    expect(within(await desktopTable()).getByText('WO-20260802-008')).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(confirmButton).toBeDisabled();
    await userEvent.click(confirmButton);
    expect(mockedApi.restoreWorkOrder).toHaveBeenCalledTimes(1);

    // The server answers; the refetch is what takes the row out.
    deletedRows = [deletedComplete];
    await act(async () => {
      resolveRestore({ message: 'Work order restored', skipped_material_allocations: [] });
    });

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await waitFor(() => expect(screen.queryAllByText('WO-20260802-008')).toHaveLength(0));
    expect(await archiveRow('WO-20260801-007')).toBeInTheDocument();
    // A clean, non-terminal restore is a plain success — role="status", green.
    expect(document.querySelector('.bg-green-600')).not.toBeNull();
    expect(document.querySelector('.bg-amber-600')).toBeNull();
  });

  it('raises a WARNING toast — not success — when the restore could not re-open a material tie', async () => {
    // THE partial-result rule. The job comes back with no demand for that material:
    // no shortage shows, stock is never deducted, and nobody finds out until a
    // count disagrees. `success` would hide exactly that.
    mockedApi.restoreWorkOrder.mockResolvedValue({
      message: 'Work order restored',
      skipped_material_allocations: [
        { allocation_id: 55, part_id: 900, work_order_operation_id: 4001, reason: 'part_not_tieable' },
      ],
    });

    renderAt('/work-orders?tab=deleted');
    await archiveRow('WO-20260802-008');
    deletedRows = [deletedComplete];

    const confirmButton = await openRestoreDialog('WO-20260802-008');
    await userEvent.click(confirmButton);

    // Assert the VARIANT, not just the words: a refactor that collapses this branch
    // into `success` keeps the same sentence and changes only these two.
    const toast = await screen.findByRole('alert');
    expect(toast).toHaveTextContent(/1 material tie did not come back/i);
    expect(toast).toHaveClass('bg-amber-600');
    expect(toast).not.toHaveClass('bg-red-600');
    expect(document.querySelector('.bg-green-600')).toBeNull();
  });

  it('raises a WARNING toast when a restored terminal-status job lands on no list', async () => {
    // The archive lists complete/closed/cancelled; the default orders list excludes
    // exactly those. So this restore succeeds and the job appears on NEITHER tab —
    // a partial result the planner has to be told about.
    mockedApi.restoreWorkOrder.mockResolvedValue({
      message: 'Work order restored',
      skipped_material_allocations: [],
    });

    renderAt('/work-orders?tab=deleted');
    await archiveRow('WO-20260801-007');
    deletedRows = [deletedDraft];

    const confirmButton = await openRestoreDialog('WO-20260801-007');
    await userEvent.click(confirmButton);

    const toast = await screen.findByRole('alert');
    expect(toast).toHaveTextContent(/WO-20260801-007 restored/);
    expect(toast).toHaveTextContent(/complete.*default work order list/i);
    expect(toast).toHaveClass('bg-amber-600');
    expect(document.querySelector('.bg-green-600')).toBeNull();
  });

  it('discloses the terminal-status consequence BEFORE the click, in the dialog', async () => {
    renderAt('/work-orders?tab=deleted');
    await archiveRow('WO-20260801-007');

    await openRestoreDialog('WO-20260801-007');

    expect(screen.getByRole('dialog')).toHaveTextContent(
      /it is complete, so it will not appear on the default work order list/i
    );
  });

  it('surfaces the server detail verbatim on refusal and leaves the row in the archive', async () => {
    mockedApi.restoreWorkOrder.mockRejectedValue({
      response: { status: 409, data: { detail: 'Work order is not deleted' } },
    });

    renderAt('/work-orders?tab=deleted');
    await archiveRow('WO-20260802-008');

    const confirmButton = await openRestoreDialog('WO-20260802-008');
    await userEvent.click(confirmButton);

    const toast = await screen.findByRole('alert');
    expect(toast).toHaveTextContent('Work order is not deleted');
    expect(toast).toHaveClass('bg-red-600');
    // Nothing was changed, so nothing moves: the row is still restorable and the
    // dialog stays up so a retry is a decision rather than a re-hunt.
    expect(within(await desktopTable()).getByText('WO-20260802-008')).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    // No success/warning toast alongside the failure.
    expect(document.querySelector('.bg-green-600')).toBeNull();
    expect(document.querySelector('.bg-amber-600')).toBeNull();
  });
});
