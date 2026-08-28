/**
 * WorkOrderTemplatesPanel — the pointer from a template's deleted source job to
 * the screen that brings that JOB back.
 *
 * A template is a NAME plus a POINTER at a work order, and it must keep working
 * when that work order is deleted: the source row can only ever be SOFT-deleted
 * (NOT NULL FK, no `ON DELETE`), so the plan is read straight through it, the
 * counts are real and Use is enabled. The row therefore carries a MUTED note, not
 * the red refusal it used to — there is nothing here for the planner to fix.
 *
 * Two properties are locked here, and they are a pair:
 *
 * 1. A user who can restore gets a LINK at `/work-orders?tab=deleted` — the tab
 *    param the page actually reads, not a route (`/work-orders/deleted` resolves
 *    as a work order whose id is the word "deleted"). It is there for the reader
 *    who wants the JOB back, which is a different want from using the template.
 * 2. A user who CANNOT restore gets the note alone, with NO link — the tab falls
 *    back to the orders list for them, so a link would be a dead end.
 *
 * The prop is passed in rather than read from `useAuth` so the panel stays a
 * presentation component; its default is the narrower answer, which is why the
 * neighbouring suite (which renders with no Router) is unaffected. This file wraps
 * in a MemoryRouter because the permissive branch renders a react-router `<Link>`.
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import WorkOrderTemplatesPanel from './WorkOrderTemplatesPanel';
import api from '../../services/api';
import { ToastProvider } from '../ui';
import type { WorkOrderTemplate } from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    listWorkOrderTemplates: jest.fn(),
    updateWorkOrderTemplate: jest.fn(),
    deleteWorkOrderTemplate: jest.fn(),
    useWorkOrderTemplate: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

/**
 * A template whose source work order was soft-deleted. The plan is read THROUGH
 * that work order, so everything below is populated exactly as it would be for a
 * live source — that is the whole point of the change.
 */
const DELETED_SOURCE_TEMPLATE: WorkOrderTemplate = {
  id: 9,
  name: 'Old weld fixture',
  notes: null,
  source_work_order_id: 42,
  default_quantity: 12,
  created_at: '2026-08-20T12:00:00Z',
  updated_at: '2026-08-20T12:00:00Z',
  created_by: 3,
  plan: {
    available: true,
    unavailable_reason: null,
    source_work_order_deleted: true,
    source_work_order_number: 'WO-20260420-007',
    source_status: 'complete',
    work_order_type: 'production',
    sequential_operations: true,
    priority: 3,
    operation_count: 4,
    nest_count: 0,
    planned_runs_total: 0,
    open_material_tie_count: 0,
    work_centers: ['WELD-1'],
    source_quantity_ordered: 12,
  },
};

/**
 * A template that IS unusable, for a reason that is not a deletion. The pointer
 * hangs off `source_work_order_deleted`, never off "unavailable" — sending a
 * planner to an archive that cannot contain the fix is a wrong instruction, not a
 * harmless extra.
 */
const UNAVAILABLE_TEMPLATE: WorkOrderTemplate = {
  ...DELETED_SOURCE_TEMPLATE,
  id: 10,
  name: 'Mystery template',
  plan: {
    ...DELETED_SOURCE_TEMPLATE.plan,
    available: false,
    unavailable_reason: 'some_future_reason',
    source_work_order_deleted: false,
  },
};

function renderPanel(props: { canRestoreWorkOrders?: boolean } = {}) {
  return render(
    <ToastProvider>
      <MemoryRouter>
        <WorkOrderTemplatesPanel onUsed={jest.fn()} {...props} />
      </MemoryRouter>
    </ToastProvider>
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.listWorkOrderTemplates.mockResolvedValue({ templates: [DELETED_SOURCE_TEMPLATE], total: 1 });
});

describe('WorkOrderTemplatesPanel: the deleted-source note points at the Deleted tab', () => {
  it('links a restorer straight at ?tab=deleted', async () => {
    renderPanel({ canRestoreWorkOrders: true });

    const note = await screen.findByTestId('template-source-deleted-9');
    const link = within(note).getByRole('link', { name: /Deleted tab/i });
    // The TAB param, not a route: /work-orders/deleted is matched by the
    // /work-orders/:id route and would resolve as a work order named "deleted".
    expect(link).toHaveAttribute('href', '/work-orders?tab=deleted');
  });

  it('gives the note alone — and no dead link — when the reader cannot restore', async () => {
    // The Deleted tab falls back to the orders list for this population, so a link
    // here would land them somewhere that silently is not what it promised. The
    // note still stands on its own: it is context, not an instruction.
    renderPanel({ canRestoreWorkOrders: false });

    const note = await screen.findByTestId('template-source-deleted-9');
    expect(note).toHaveTextContent(/source work order was deleted/i);
    expect(within(note).queryByRole('link')).not.toBeInTheDocument();
  });

  it('defaults to the narrower wording when the prop is omitted', async () => {
    // The default has to be the safe one: the panel is rendered by callers that do
    // not know the reader's role, and a link nobody can use is worse than a name.
    renderPanel();

    const note = await screen.findByTestId('template-source-deleted-9');
    expect(within(note).queryByRole('link')).not.toBeInTheDocument();
  });

  it('adds no pointer, and no deleted note, to a template unavailable for another reason', async () => {
    mockApi.listWorkOrderTemplates.mockResolvedValue({
      templates: [UNAVAILABLE_TEMPLATE],
      total: 1,
    });
    renderPanel({ canRestoreWorkOrders: true });

    const reason = await screen.findByTestId('template-unavailable-10');
    expect(screen.queryByTestId('template-source-deleted-10')).not.toBeInTheDocument();
    expect(within(reason).queryByRole('link')).not.toBeInTheDocument();
    expect(reason).not.toHaveTextContent(/Deleted tab/i);
  });
});
