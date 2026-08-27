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
import type { WorkOrderRestoreResponse, WorkOrderSummary, WorkOrderTemplate } from '../types';

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

/**
 * Deleted, OVERDUE and IN PROGRESS — built so that a leak into `workOrders` would be
 * visible in the numbers rather than only in the table: it would add an Overdue, an
 * In Progress, a row to the "showing X of Y" counter and a customer to the filter.
 */
const deletedOverdue: WorkOrderSummary = {
  id: 9,
  work_order_number: 'WO-20260803-009',
  part_id: 90,
  work_order_type: 'production',
  part_number: 'PN-LATE',
  part_name: 'Manifold',
  part_type: 'manufactured',
  status: 'in_progress',
  priority: 1,
  quantity_ordered: 30,
  quantity_complete: 5,
  customer_name: 'Gamma Metalworks',
  due_date: '2020-01-15',
  is_deleted: true,
  deleted_at: '2026-08-24T18:00:00Z',
  deleted_by_name: 'Dana Reyes',
};

/**
 * A template whose source work order was soft-deleted. Its row names restoring the
 * work order as one of the two fixes, and the wording depends on a prop this page
 * has to pass down (see the `canRestoreWorkOrders` block at the bottom).
 */
const deadTemplate: WorkOrderTemplate = {
  id: 9,
  name: 'Old weld fixture',
  notes: null,
  source_work_order_id: 42,
  default_quantity: 12,
  created_at: '2026-08-20T12:00:00Z',
  updated_at: '2026-08-20T12:00:00Z',
  created_by: 3,
  plan: {
    available: false,
    unavailable_reason: 'source_work_order_deleted',
    source_work_order_number: null,
    source_status: null,
    work_order_type: null,
    sequential_operations: null,
    priority: null,
    operation_count: 0,
    nest_count: 0,
    planned_runs_total: 0,
    open_material_tie_count: 0,
    work_centers: [],
    source_quantity_ordered: null,
  },
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

/** Same, for assertions that must NOT await (a stale response must not be waited in). */
const desktopTableSync = (): HTMLElement => screen.getAllByTestId('data-table')[0] as HTMLElement;

/** The orders tab's three MiniStat values, in strip order: Overdue, In Progress, Due Today. */
const statValues = (): string[] =>
  Array.from(document.querySelectorAll('.stat-value')).map((el) => el.textContent ?? '');

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
    // Columns: Work Order | Part | Customer | Status | Qty | Due Date | Deleted |
    // Deleted By | Actions
    expect(cells[7]).toHaveTextContent('—');
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

  it('surfaces the server detail verbatim on a 4xx refusal, and closes the dialog WITH the row it names', async () => {
    // A 4xx is the server saying this table is wrong — overwhelmingly "somebody else
    // restored it already". The catch re-reads the archive, which drops the phantom
    // row; a dialog left open then names a work order that is no longer in the table,
    // with an enabled Restore button whose every retry can only 400 again.
    mockedApi.restoreWorkOrder.mockRejectedValue({
      response: { status: 409, data: { detail: 'Work order is not deleted' } },
    });

    renderAt('/work-orders?tab=deleted');
    await archiveRow('WO-20260802-008');
    // What the re-read finds: somebody else already restored it.
    deletedRows = [deletedComplete];

    const confirmButton = await openRestoreDialog('WO-20260802-008');
    await userEvent.click(confirmButton);

    const toast = await screen.findByRole('alert');
    expect(toast).toHaveTextContent('Work order is not deleted');
    expect(toast).toHaveClass('bg-red-600');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await waitFor(() => expect(screen.queryAllByText('WO-20260802-008')).toHaveLength(0));
    // No success/warning toast alongside the failure.
    expect(document.querySelector('.bg-green-600')).toBeNull();
    expect(document.querySelector('.bg-amber-600')).toBeNull();
  });

  it('keeps the dialog up — and the row — on a 5xx, where the row is still real', async () => {
    // The other half of the split. A 500 (or an offline `err.response === undefined`)
    // says nothing about the row: it is still deleted and still restorable, so the
    // retry has to stay one click away rather than a re-hunt through the archive.
    // Deliberately no re-read either — a failing one would swap the archive for an
    // error state over a transient blip.
    mockedApi.restoreWorkOrder.mockRejectedValue({
      response: { status: 500, data: { detail: 'Internal Server Error' } },
    });

    renderAt('/work-orders?tab=deleted');
    await archiveRow('WO-20260802-008');
    const fetchesBefore = deletedFetchCalls().length;

    const confirmButton = await openRestoreDialog('WO-20260802-008');
    await userEvent.click(confirmButton);

    const toast = await screen.findByRole('alert');
    expect(toast).toHaveClass('bg-red-600');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(within(await desktopTable()).getByText('WO-20260802-008')).toBeInTheDocument();
    expect(deletedFetchCalls()).toHaveLength(fetchesBefore);
    // ...and the retry is live, not stuck disabled behind `restorePending`.
    await waitFor(() =>
      expect(within(screen.getByRole('dialog')).getByRole('button', { name: 'Restore' })).toBeEnabled()
    );
  });
});

describe('WorkOrders: the archive read is latched and re-run on every entry', () => {
  it('lets the NEWEST archive request win, however late the older one lands', async () => {
    // Deleted -> Orders -> Deleted leaves two reads in flight. Without the
    // `deletedRequestRef` latch the SLOWER (older) response paints last and the user
    // is looking at an archive from before whatever they left the tab to do — the
    // exact staleness that re-fetching on every entry exists to prevent, reached by a
    // different road.
    const pending: Array<(rows: WorkOrderSummary[]) => void> = [];
    mockedApi.getWorkOrders.mockImplementation(async (params?: { deleted_only?: boolean }) => {
      if (!params?.deleted_only) return [liveWorkOrder];
      return new Promise<WorkOrderSummary[]>((resolve) => {
        pending.push(resolve);
      });
    });

    renderAt('/work-orders?tab=deleted');
    await waitFor(() => expect(pending).toHaveLength(1));

    await userEvent.click(screen.getByRole('button', { name: 'Work Orders' }));
    await userEvent.click(screen.getByRole('button', { name: 'Deleted' }));
    await waitFor(() => expect(pending).toHaveLength(2));

    // The SECOND (current) read answers first.
    await act(async () => {
      pending[1]([deletedDraft]);
    });
    expect(await archiveRow('WO-20260802-008')).toBeInTheDocument();

    // ...and the first, superseded one lands afterwards carrying different rows.
    await act(async () => {
      pending[0]([deletedComplete]);
    });
    expect(within(desktopTableSync()).getByText('WO-20260802-008')).toBeInTheDocument();
    expect(screen.queryAllByText('WO-20260801-007')).toHaveLength(0);
  });

  it('re-reads the archive on RE-entry, not just the first time', async () => {
    // Somebody else may have deleted or restored a work order in between; a cached
    // archive is how you end up clicking Restore on a row the server will refuse.
    renderAt('/work-orders?tab=deleted');
    await archiveRow('WO-20260801-007');
    expect(deletedFetchCalls()).toHaveLength(1);

    await userEvent.click(screen.getByRole('button', { name: 'Work Orders' }));
    await screen.findAllByText('WO-1001');
    // Leaving does NOT re-read — the archive stays out of the orders tab's traffic.
    expect(deletedFetchCalls()).toHaveLength(1);

    // A row this session never saw was deleted while the user was away.
    deletedRows = [deletedComplete, deletedDraft, deletedOverdue];
    await userEvent.click(screen.getByRole('button', { name: 'Deleted' }));

    expect(await archiveRow('WO-20260803-009')).toBeInTheDocument();
    expect(deletedFetchCalls()).toHaveLength(2);
  });

  it('shows the loading state, never an "empty archive", before the first read answers', async () => {
    // `deletedLoading` starts TRUE because the read fires from a passive effect after
    // the first paint. Started false, DataTable's `!loading && !isError && !rows`
    // empty state paints "No deleted work orders" over an archive that is on its way
    // — the one sentence that makes someone stop looking.
    let resolveArchive: (rows: WorkOrderSummary[]) => void = () => {};
    mockedApi.getWorkOrders.mockImplementation(async (params?: { deleted_only?: boolean }) => {
      if (!params?.deleted_only) return [liveWorkOrder];
      return new Promise<WorkOrderSummary[]>((resolve) => {
        resolveArchive = resolve;
      });
    });

    renderAt('/work-orders?tab=deleted');

    // The banner is up, so the branch has rendered — and it is not claiming the
    // archive is empty.
    expect(await screen.findByText(/These work orders are deleted records/)).toBeInTheDocument();
    expect(screen.queryByText('No deleted work orders')).not.toBeInTheDocument();

    await act(async () => {
      resolveArchive([deletedComplete]);
    });
    expect(await archiveRow('WO-20260801-007')).toBeInTheDocument();
  });

  it('renders the archive error state with a Retry that re-reads', async () => {
    mockedApi.getWorkOrders.mockImplementation(async (params?: { deleted_only?: boolean }) => {
      if (!params?.deleted_only) return [liveWorkOrder];
      throw new Error('boom');
    });

    renderAt('/work-orders?tab=deleted');

    // A blank section (or an empty state) would read as "nothing was ever deleted" —
    // the opposite of what happened.
    expect(
      await screen.findAllByText('Could not load deleted work orders.')
    ).not.toHaveLength(0);
    expect(screen.queryByText('No deleted work orders')).not.toBeInTheDocument();
    expect(deletedFetchCalls()).toHaveLength(1);

    mockedApi.getWorkOrders.mockImplementation(async (params?: { deleted_only?: boolean }) =>
      params?.deleted_only ? deletedRows : [liveWorkOrder]
    );
    await userEvent.click(screen.getAllByRole('button', { name: /retry/i })[0]);

    expect(await archiveRow('WO-20260801-007')).toBeInTheDocument();
    expect(deletedFetchCalls()).toHaveLength(2);
  });
});

describe('WorkOrders: the archive is structurally separate from the orders book', () => {
  it('leaves every orders-tab count and KPI untouched after the archive loads', async () => {
    // `deletedWorkOrders` is its OWN state and never merges into `workOrders`. That is
    // the whole guarantee: the customer options, the filters, the groupings, the
    // "showing X of Y" counter and all three MiniStats derive from `workOrders`, so a
    // deleted row landing there would count a job somebody deleted as overdue and in
    // progress. `deletedOverdue` is built to be visible in exactly those numbers if it
    // ever leaks.
    deletedRows = [deletedComplete, deletedDraft, deletedOverdue];
    renderAt('/work-orders');
    await screen.findAllByText('WO-1001');

    const before = statValues();
    const countBefore = screen.getByText(/work orders$/).textContent;

    await userEvent.click(screen.getByRole('button', { name: 'Deleted' }));
    await archiveRow('WO-20260803-009');
    await userEvent.click(screen.getByRole('button', { name: 'Work Orders' }));
    await screen.findAllByText('WO-1001');

    expect(statValues()).toEqual(before);
    expect(screen.getByText(/work orders$/).textContent).toBe(countBefore);
    // State the numbers outright too, so a refactor cannot make this pass by zeroing
    // the strip: Overdue / In Progress / Due Today, none of which the live row moves.
    expect(before).toEqual(['0', '0', '0']);
    // And the deleted row is nowhere on the orders tab — not in the table, and not in
    // the customer filter it would otherwise contribute an option to.
    expect(screen.queryAllByText('WO-20260803-009')).toHaveLength(0);
    expect(screen.queryByRole('option', { name: 'Gamma Metalworks' })).not.toBeInTheDocument();
  });
});

describe('WorkOrders: finding a row in an archive that only grows', () => {
  it('filters the archive client-side on WO number, part and customer', async () => {
    // Nothing ages out of the archive and it keeps the terminal-status rows the live
    // list drops, so within a year finding one job is paging 25 at a time. Filtering
    // rows already in hand keeps that off the wire — no second race surface on a read
    // that is already latched (Purchasing's deleted-PO book does the same).
    deletedRows = [deletedComplete, deletedDraft, deletedOverdue];
    renderAt('/work-orders?tab=deleted');
    await archiveRow('WO-20260801-007');

    const box = screen.getByRole('textbox', { name: 'Search deleted work orders' });
    const fetchesBefore = deletedFetchCalls().length;

    // By work order number.
    await userEvent.type(box, '008');
    await waitFor(() => expect(screen.queryAllByText('WO-20260801-007')).toHaveLength(0));
    expect(within(desktopTableSync()).getByText('WO-20260802-008')).toBeInTheDocument();

    // By part number — case-insensitively.
    await userEvent.clear(box);
    await userEvent.type(box, 'pn-dead');
    await waitFor(() => expect(screen.queryAllByText('WO-20260802-008')).toHaveLength(0));
    expect(within(desktopTableSync()).getByText('WO-20260801-007')).toBeInTheDocument();

    // By part NAME.
    await userEvent.clear(box);
    await userEvent.type(box, 'spacer');
    await waitFor(() => expect(screen.queryAllByText('WO-20260801-007')).toHaveLength(0));
    expect(within(desktopTableSync()).getByText('WO-20260802-008')).toBeInTheDocument();

    // By customer.
    await userEvent.clear(box);
    await userEvent.type(box, 'Gamma');
    await waitFor(() => expect(screen.queryAllByText('WO-20260802-008')).toHaveLength(0));
    expect(within(desktopTableSync()).getByText('WO-20260803-009')).toBeInTheDocument();

    // Client-side throughout: not one extra request.
    expect(deletedFetchCalls()).toHaveLength(fetchesBefore);
  });

  it('says the SEARCH came up empty, not that nothing was ever deleted', async () => {
    renderAt('/work-orders?tab=deleted');
    await archiveRow('WO-20260801-007');

    await userEvent.type(
      screen.getByRole('textbox', { name: 'Search deleted work orders' }),
      'no-such-job'
    );

    // Twice: DataTable mounts the desktop empty state and the mobile one in jsdom.
    expect(await screen.findAllByText('No matching deleted work orders')).not.toHaveLength(0);
    // The other sentence would send someone looking somewhere else for a job that is
    // right here behind a filter they typed.
    expect(screen.queryByText('No deleted work orders')).not.toBeInTheDocument();
  });

  it('keeps the archive filter out of the orders tab, and vice versa', async () => {
    // Two boxes, two states. Shared state would mean a term typed on one tab silently
    // hides rows on the other — and the orders one is a SERVER param, so it would also
    // re-query the wrong book.
    renderAt('/work-orders?tab=deleted');
    await archiveRow('WO-20260801-007');
    await userEvent.type(
      screen.getByRole('textbox', { name: 'Search deleted work orders' }),
      'PN-DEAD'
    );
    await waitFor(() => expect(screen.queryAllByText('WO-20260802-008')).toHaveLength(0));

    await userEvent.click(screen.getByRole('button', { name: 'Work Orders' }));
    const liveBox = await screen.findByRole('textbox', { name: 'Search work orders' });
    expect(liveBox).toHaveValue('');
    expect(await screen.findAllByText('WO-1001')).not.toHaveLength(0);
    // No archive term reached the SERVER read of the live book.
    expect(mockedApi.getWorkOrders.mock.calls.some((call) => call[0]?.search === 'PN-DEAD')).toBe(
      false
    );
  });

  it('shows the customer on an archived row, and gives it a sortable column', async () => {
    // "Whose job was it" is a primary way people identify a work order they are
    // hunting for; the live table has always had the column, and without it here the
    // archive's CSV export drops it too.
    renderAt('/work-orders?tab=deleted');
    const row = await archiveRow('WO-20260801-007');

    expect(within(row).getByText('Beta Defense')).toBeInTheDocument();
    expect(
      within(await desktopTable()).getByRole('button', { name: /^Customer/ })
    ).toBeInTheDocument();
  });
});

describe('WorkOrders: the archive banner and the confirm dialog say the same thing', () => {
  it('hedges the material-tie promise the way the dialog does', async () => {
    // A tie the delete cancelled and a later nest re-import then DETACHED comes back
    // cancelled, and the restore response reports NO skip for it — so the toast cannot
    // tell anyone. An unqualified "they come back with it" is then the last thing the
    // user reads before the job runs with no demand for that material.
    renderAt('/work-orders?tab=deleted');
    const banner = await screen.findByText(/These work orders are deleted records/);

    expect(banner).toHaveTextContent(/re-opened where they still can be/i);
    expect(banner).not.toHaveTextContent(/come back with it/i);

    // ...and the dialog, which already got this right, still agrees with it.
    await archiveRow('WO-20260802-008');
    await openRestoreDialog('WO-20260802-008');
    expect(screen.getByRole('dialog')).toHaveTextContent(/re-opened where they still can be/i);
  });
});

describe('WorkOrders: the restore dialog does not outlive the view', () => {
  it('closes when the user leaves the Deleted tab', async () => {
    // `?tab=` is a search param on the SAME route, so the page stays mounted across a
    // tab switch and `restoreTarget` would survive it: come back and the dialog
    // remounts open, pulling focus and naming a work order the user stopped thinking
    // about two tabs ago.
    renderAt('/work-orders?tab=deleted');
    await archiveRow('WO-20260802-008');
    await openRestoreDialog('WO-20260802-008');

    await userEvent.click(screen.getByRole('button', { name: 'Work Orders' }));
    await screen.findAllByText('WO-1001');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Deleted' }));
    await archiveRow('WO-20260802-008');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mockedApi.restoreWorkOrder).not.toHaveBeenCalled();
  });
});

describe('WorkOrders: the header actions work on every tab that shows them', () => {
  it('opens the nest-import wizard from the Deleted tab', async () => {
    // The header — Import Nest Package included — is rendered by all three branches,
    // but the wizard used to be mounted only below the orders return, so the button
    // was live and inert on Templates and Deleted. Everyone who can see either tab
    // clears `canImportNests`, so it was inert for all of them.
    renderAt('/work-orders?tab=deleted');
    await archiveRow('WO-20260801-007');

    await userEvent.click(screen.getByRole('button', { name: /import nest package/i }));

    expect(
      await screen.findByText(/creates a new released laser cutting work order/i)
    ).toBeInTheDocument();
  });

  it('opens the nest-import wizard from the Templates tab', async () => {
    // Same hole, shipped one commit earlier. Fixing it in one place is the point.
    renderAt('/work-orders?tab=templates');
    await screen.findByRole('button', { name: 'Templates', current: 'page' });

    await userEvent.click(screen.getByRole('button', { name: /import nest package/i }));

    expect(
      await screen.findByText(/creates a new released laser cutting work order/i)
    ).toBeInTheDocument();
  });
});

describe('WorkOrders: the Templates panel is told who can restore', () => {
  it('wires canRestoreWorkOrders through, so a dead template LINKS at the archive', async () => {
    // The four tests that pin this pointer render the panel directly and pass the prop
    // themselves, so dropping the wiring HERE degrades in total silence: every admin
    // would get the "an admin or manager can restore it" sentence — advice to go ask
    // themselves — because the prop defaults to the narrower answer.
    mockedApi.listWorkOrderTemplates.mockResolvedValue({ templates: [deadTemplate], total: 1 });

    renderAt('/work-orders?tab=templates');

    const link = await screen.findByRole('link', { name: 'Find it on the Deleted tab.' });
    expect(link).toHaveAttribute('href', '/work-orders?tab=deleted');
  });

  it('gives a SUPERVISOR the sentence instead — they hold work_orders:edit but not delete', async () => {
    // The gates genuinely differ: Templates is work_orders:edit (admin/manager/
    // supervisor), the archive is admin/manager (+superuser). A link would be a dead
    // end — `?tab=deleted` falls back to the orders list for them.
    mockUser.current = { id: 4, role: 'supervisor', is_superuser: false };
    mockedApi.listWorkOrderTemplates.mockResolvedValue({ templates: [deadTemplate], total: 1 });

    renderAt('/work-orders?tab=templates');

    expect(
      await screen.findByText(/an admin or manager can restore it from the Deleted tab/i)
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('link', { name: 'Find it on the Deleted tab.' })
    ).not.toBeInTheDocument();
  });
});
