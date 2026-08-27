/**
 * WorkOrderTemplatesPanel — the pointer from a dead template to the screen that
 * fixes it.
 *
 * A template is a NAME plus a POINTER at a work order. When that work order is
 * soft-deleted the template is still listed (hiding it is the mask trap invariant
 * 3 documents) and its row names the cause. There are exactly two fixes: delete the
 * template, or RESTORE the work order — and until the Deleted tab existed the
 * second one named an action with no screen behind it.
 *
 * Two properties are locked here, and they are a pair:
 *
 * 1. A user who can restore gets a LINK at `/work-orders?tab=deleted` — the tab
 *    param the page actually reads, not a route (`/work-orders/deleted` resolves
 *    as a work order whose id is the word "deleted").
 * 2. A user who CANNOT restore gets the sentence naming who can, and NO link — the
 *    tab falls back to the orders list for them, so a link would be a dead end.
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

/** A template whose source work order was soft-deleted — plan unavailable. */
const DEAD_TEMPLATE: WorkOrderTemplate = {
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

/**
 * A template that is unusable for some OTHER reason. The `unavailable_reason`
 * vocabulary is the server's and is OPEN, so the pointer must be a match on the
 * one token whose fix is the Deleted tab — never an assumption that "unavailable"
 * means "deleted".
 */
const OTHER_REASON_TEMPLATE: WorkOrderTemplate = {
  ...DEAD_TEMPLATE,
  id: 10,
  name: 'Mystery template',
  plan: { ...DEAD_TEMPLATE.plan, unavailable_reason: 'some_future_reason' },
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
  mockApi.listWorkOrderTemplates.mockResolvedValue({ templates: [DEAD_TEMPLATE], total: 1 });
});

describe('WorkOrderTemplatesPanel: the dead-source line points at the Deleted tab', () => {
  it('links a restorer straight at ?tab=deleted', async () => {
    renderPanel({ canRestoreWorkOrders: true });

    const reason = await screen.findByTestId('template-unavailable-9');
    const link = within(reason).getByRole('link', { name: /Deleted tab/i });
    // The TAB param, not a route: /work-orders/deleted is matched by the
    // /work-orders/:id route and would resolve as a work order named "deleted".
    expect(link).toHaveAttribute('href', '/work-orders?tab=deleted');
  });

  it('names who can do it — and offers no dead link — when the reader cannot restore', async () => {
    // The Deleted tab falls back to the orders list for this population, so a link
    // here would land them somewhere that silently is not what it promised.
    renderPanel({ canRestoreWorkOrders: false });

    const reason = await screen.findByTestId('template-unavailable-9');
    expect(reason).toHaveTextContent(/An admin or manager can restore it from the Deleted tab/i);
    expect(within(reason).queryByRole('link')).not.toBeInTheDocument();
  });

  it('defaults to the narrower wording when the prop is omitted', async () => {
    // The default has to be the safe one: the panel is rendered by callers that do
    // not know the reader's role, and a link nobody can use is worse than a name.
    renderPanel();

    const reason = await screen.findByTestId('template-unavailable-9');
    expect(within(reason).queryByRole('link')).not.toBeInTheDocument();
  });

  it('adds no pointer to an unavailable template with a DIFFERENT reason', async () => {
    // `unavailable_reason` is an OPEN vocabulary. Restoring a work order fixes
    // exactly one token; sending a planner to an archive that cannot contain the
    // fix is a wrong instruction, not a harmless extra.
    mockApi.listWorkOrderTemplates.mockResolvedValue({
      templates: [OTHER_REASON_TEMPLATE],
      total: 1,
    });
    renderPanel({ canRestoreWorkOrders: true });

    const reason = await screen.findByTestId('template-unavailable-10');
    expect(within(reason).queryByRole('link')).not.toBeInTheDocument();
    expect(reason).not.toHaveTextContent(/Deleted tab/i);
  });
});
