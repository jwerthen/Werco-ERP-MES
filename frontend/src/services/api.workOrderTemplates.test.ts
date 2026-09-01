/**
 * Work-order-template client boundary — the wire shapes, pinned at their source.
 *
 * This file exists for the reason `api.duplicateWorkOrder.test.ts` exists: the
 * component and page suites MOCK `services/api`, so every assertion they make is
 * against the DECLARED return type of these methods, not against what the server
 * sends. A mismatch between the two is invisible there by construction — that is
 * the SPC failure mode (CLAUDE.md → type-check), and it is how the duplicate
 * envelope once shipped as "undefined created as a draft" with a green suite.
 *
 * Six contracts are pinned here, and each has a way of going wrong quietly:
 *
 *  1. **`GET /work-order-templates` returns an ENVELOPE**, `{ templates, total }`.
 *     Unwrapping it to a bare array would compile and render an empty catalog.
 *  2. **`POST /{id}/use` returns a STRICT SUPERSET of the duplicate envelope**, not
 *     a bare work order — the skip lists are safety information (a skipped material
 *     tie means the new job carries no demand for that material: no shortage shows,
 *     the nests run, stock is never deducted) — plus `created_count` /
 *     `work_orders` for a batch, with `work_orders[0]` the same row as
 *     `work_order`.
 *  3. **`use` omits `quantity_ordered` rather than sending null** when the caller
 *     has none. Omitted is what lets the server resolve the template's default and
 *     then the source work order's own quantity; a fabricated number would be a
 *     plan nobody approved.
 *  4. **`use` sends an explicit `due_date: null`** when blank. Unscheduled is a
 *     decision — the source's date is never inherited.
 *  5. **`use` omits `count` and `unit_numbers` for a SINGLE use.** The server body
 *     is `extra="forbid"`, and the one-at-a-time path is what every deployed client
 *     already exercises: it must not start carrying new keys because a batch
 *     feature exists beside it.
 *  6. **`use` sends the unit list POSITIONALLY, nulls included.** A null entry is
 *     "this work order has no unit yet"; compacting it out would shift every unit
 *     after it onto the wrong job.
 *
 * axios is mocked at the module boundary (same pattern as api.spc.test.ts).
 */

const mockGet = jest.fn();
const mockPost = jest.fn();
const mockPut = jest.fn();
const mockPatch = jest.fn();
const mockDelete = jest.fn();

const mockAxiosInstance = {
  get: mockGet,
  post: mockPost,
  put: mockPut,
  patch: mockPatch,
  delete: mockDelete,
  defaults: { headers: { common: {} as Record<string, string> } },
  interceptors: {
    request: { use: jest.fn() },
    response: { use: jest.fn() },
  },
};

jest.mock('axios', () => {
  const create = jest.fn(() => mockAxiosInstance);
  return {
    __esModule: true,
    default: { create, post: jest.fn() },
    create,
  };
});

import api from './api';

const ok = (data: unknown, status = 200) => ({ status, data, headers: {} });

/** Exactly what `WorkOrderTemplateResponse` serializes to. */
const template = (overrides: Record<string, unknown> = {}) => ({
  id: 7,
  name: 'Miratech nest group',
  notes: 'Runs on the Ermaksan.',
  source_work_order_id: 42,
  default_quantity: null,
  created_at: '2026-08-20T12:00:00Z',
  updated_at: '2026-08-20T12:00:00Z',
  created_by: 3,
  plan: {
    available: true,
    unavailable_reason: null,
    source_work_order_number: 'WO-20260501-004',
    source_status: 'complete',
    work_order_type: 'laser_cutting',
    sequential_operations: false,
    priority: 3,
    operation_count: 21,
    nest_count: 21,
    planned_runs_total: 63,
    open_material_tie_count: 2,
    work_centers: ['LASER-1', 'BRAKE-2'],
    source_quantity_ordered: 63,
  },
  ...overrides,
});

/**
 * The envelope `POST /{id}/use` answers with — a STRICT SUPERSET of the duplicate
 * one, mirroring the server schema that SUBCLASSES it. `work_orders[0]` IS
 * `work_order`, and `created_count` equals the list length. Both are stamped for a
 * SINGLE use too, so there is one shape to read rather than keys that appear only
 * sometimes — and a fixture that let the singular field and the list disagree would
 * let a component test pass against a shape the server never sends.
 */
const useWorkOrder = {
  id: 501,
  version: 1,
  work_order_number: 'WO-20260825-002',
  part_id: 10,
  work_order_type: 'laser_cutting',
  quantity_ordered: 63,
  quantity_complete: 0,
  quantity_scrapped: 0,
  status: 'draft',
  priority: 3,
  estimated_hours: 0,
  actual_hours: 0,
  estimated_cost: 0,
  actual_cost: 0,
  created_at: '2026-08-25T12:00:00Z',
  updated_at: '2026-08-25T12:00:00Z',
  operations: [],
};

const useEnvelope = (overrides: Record<string, unknown> = {}) => ({
  work_order: useWorkOrder,
  created_count: 1,
  work_orders: [useWorkOrder],
  skipped_operations: [],
  skipped_material_allocations: [],
  ...overrides,
});

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockPut.mockReset();
  mockDelete.mockReset();
});

describe('api.listWorkOrderTemplates', () => {
  it('reads the catalog route and returns the ENVELOPE, not a bare array', async () => {
    mockGet.mockResolvedValue(ok({ templates: [template()], total: 1 }));

    const result = await api.listWorkOrderTemplates();

    expect(mockGet).toHaveBeenCalledWith('/work-order-templates', { params: undefined });
    // Unwrapping to `response.data.templates` would compile and would drop
    // `total`; returning `response.data` on a changed shape would render empty.
    expect(result).toHaveProperty('templates');
    expect(result.total).toBe(1);
    expect(result.templates).toHaveLength(1);
  });

  it('carries the LIVE plan summary through — it is what the picker is read by', async () => {
    mockGet.mockResolvedValue(ok({ templates: [template()], total: 1 }));

    const [row] = (await api.listWorkOrderTemplates()).templates;

    expect(row.plan.source_work_order_number).toBe('WO-20260501-004');
    expect(row.plan.nest_count).toBe(21);
    expect(row.plan.planned_runs_total).toBe(63);
    expect(row.plan.open_material_tie_count).toBe(2);
    expect(row.plan.work_centers).toEqual(['LASER-1', 'BRAKE-2']);
    // false = a same-work-center dispatch POOL, not a missing value.
    expect(row.plan.sequential_operations).toBe(false);
  });

  it('sends the search term as a query param, and omits it when blank', async () => {
    mockGet.mockResolvedValue(ok({ templates: [], total: 0 }));

    await api.listWorkOrderTemplates({ search: '  nest  ' });
    expect(mockGet).toHaveBeenCalledWith('/work-order-templates', { params: { search: 'nest' } });

    await api.listWorkOrderTemplates({ search: '   ' });
    expect(mockGet).toHaveBeenLastCalledWith('/work-order-templates', { params: undefined });
  });

  it('passes an unavailable plan through intact rather than dropping the row', async () => {
    // The server LISTS a template whose source work order was deleted, with the
    // cause. Filtering it here is the mask trap: the row would simply vanish and
    // the planner would never see the one thing that explains it.
    mockGet.mockResolvedValue(
      ok({
        templates: [
          template({
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
          }),
        ],
        total: 1,
      })
    );

    const [row] = (await api.listWorkOrderTemplates()).templates;

    expect(row.plan.available).toBe(false);
    expect(row.plan.unavailable_reason).toBe('source_work_order_deleted');
  });
});

describe('api.getWorkOrderTemplate / create / update / delete', () => {
  it('reads one template by id', async () => {
    mockGet.mockResolvedValue(ok(template()));

    const result = await api.getWorkOrderTemplate(7);

    expect(mockGet).toHaveBeenCalledWith('/work-order-templates/7');
    expect(result.name).toBe('Miratech nest group');
  });

  it('posts the create body to the collection route', async () => {
    mockPost.mockResolvedValue(ok(template(), 201));

    await api.createWorkOrderTemplate({
      source_work_order_id: 42,
      name: 'Miratech nest group',
      notes: 'Runs on the Ermaksan.',
      default_quantity: 12,
    });

    expect(mockPost).toHaveBeenCalledWith('/work-order-templates', {
      source_work_order_id: 42,
      name: 'Miratech nest group',
      notes: 'Runs on the Ermaksan.',
      default_quantity: 12,
    });
  });

  it('forwards an explicit null on update rather than dropping it', async () => {
    // `null` is MEANINGFUL to this endpoint — it CLEARS the field, while an
    // omitted key leaves the stored value alone. Normalizing nulls away here
    // would make "undo my typo" impossible from any caller.
    mockPut.mockResolvedValue(ok(template({ notes: null })));

    await api.updateWorkOrderTemplate(7, { notes: null, default_quantity: null });

    expect(mockPut).toHaveBeenCalledWith('/work-order-templates/7', {
      notes: null,
      default_quantity: null,
    });
  });

  it('sends only the keys the caller set on update', async () => {
    mockPut.mockResolvedValue(ok(template({ name: 'Bracket brake set' })));

    await api.updateWorkOrderTemplate(7, { name: 'Bracket brake set' });

    expect(mockPut).toHaveBeenCalledWith('/work-order-templates/7', { name: 'Bracket brake set' });
  });

  it('deletes by id and returns the server message', async () => {
    mockDelete.mockResolvedValue(ok({ message: "Work order template 'Miratech nest group' deleted", id: 7 }));

    const result = await api.deleteWorkOrderTemplate(7);

    expect(mockDelete).toHaveBeenCalledWith('/work-order-templates/7');
    expect(result.id).toBe(7);
  });
});

describe('api.useWorkOrderTemplate: the request', () => {
  it('OMITS quantity_ordered when the caller has none', async () => {
    // Omitted, not null and not a fabricated 1: it is what lets the server
    // resolve the template's default and then the source work order's quantity,
    // and refuse 422 if neither is positive.
    mockPost.mockResolvedValue(ok(useEnvelope(), 201));

    await api.useWorkOrderTemplate(7);

    expect(mockPost).toHaveBeenCalledWith('/work-order-templates/7/use', { due_date: null });
    const [, body] = mockPost.mock.calls[0] as [string, Record<string, unknown>];
    expect('quantity_ordered' in body).toBe(false);
  });

  it('sends an explicit null due date rather than omitting the key', async () => {
    mockPost.mockResolvedValue(ok(useEnvelope(), 201));

    await api.useWorkOrderTemplate(7, { quantity_ordered: 25 });

    expect(mockPost).toHaveBeenCalledWith('/work-order-templates/7/use', {
      due_date: null,
      quantity_ordered: 25,
    });
  });

  it('sends both fields when the planner set both', async () => {
    mockPost.mockResolvedValue(ok(useEnvelope(), 201));

    await api.useWorkOrderTemplate(7, { quantity_ordered: 25, due_date: '2026-09-30' });

    expect(mockPost).toHaveBeenCalledWith('/work-order-templates/7/use', {
      due_date: '2026-09-30',
      quantity_ordered: 25,
    });
  });

  it('OMITS count and unit_numbers for a single use, whatever the caller passes', async () => {
    // The server body is `extra="forbid"`, and this is the path every deployed
    // client already exercises: it must not start carrying new keys because a
    // batch feature exists beside it. `count: 1` and an EMPTY list are both the
    // absence of a batch, so both are omitted rather than serialized.
    mockPost.mockResolvedValue(ok(useEnvelope(), 201));

    await api.useWorkOrderTemplate(7, { quantity_ordered: 25, count: 1, unit_numbers: [] });

    expect(mockPost).toHaveBeenCalledWith('/work-order-templates/7/use', {
      due_date: null,
      quantity_ordered: 25,
    });
    const [, body] = mockPost.mock.calls[0] as [string, Record<string, unknown>];
    expect('count' in body).toBe(false);
    expect('unit_numbers' in body).toBe(false);
  });

  it('sends the count and the positional unit list for a batch, nulls included', async () => {
    // A null entry is "this work order has no unit yet", which the server stores
    // as NULL — it is not a hole to be compacted out, because the list is
    // POSITIONAL and dropping it would shift every unit after it onto the wrong
    // job.
    mockPost.mockResolvedValue(ok(useEnvelope(), 201));

    await api.useWorkOrderTemplate(7, {
      quantity_ordered: 1,
      count: 3,
      unit_numbers: ['2410048', null, 'K-9812'],
    });

    expect(mockPost).toHaveBeenCalledWith('/work-order-templates/7/use', {
      due_date: null,
      quantity_ordered: 1,
      count: 3,
      unit_numbers: ['2410048', null, 'K-9812'],
    });
  });
});

describe('api.useWorkOrderTemplate: the envelope reaches the caller intact', () => {
  it('returns the duplicate ENVELOPE, NOT a bare work order', async () => {
    mockPost.mockResolvedValue(ok(useEnvelope(), 201));

    const result = await api.useWorkOrderTemplate(7);

    expect(result).toHaveProperty('work_order');
    expect(result).toHaveProperty('skipped_operations');
    expect(result).toHaveProperty('skipped_material_allocations');
    // A bare work order would carry these at the top level instead.
    expect(result).not.toHaveProperty('work_order_number');
  });

  it('carries the batch fields, with work_orders[0] the same row as work_order', async () => {
    // A caller reading the singular field and one reading the list must never be
    // looking at two different work orders — the server builds the envelope from
    // one serialization, and the client type says so.
    const second = { ...useWorkOrder, id: 502, work_order_number: 'WO-20260825-003' };
    mockPost.mockResolvedValue(ok(useEnvelope({ created_count: 2, work_orders: [useWorkOrder, second] }), 201));

    const result = await api.useWorkOrderTemplate(7, { count: 2 });

    expect(result.created_count).toBe(2);
    expect(result.work_orders.map(workOrder => workOrder.work_order_number)).toEqual([
      'WO-20260825-002',
      'WO-20260825-003',
    ]);
    expect(result.work_orders[0].id).toBe(result.work_order.id);
  });

  it('carries the new DRAFT work order through under `work_order`', async () => {
    mockPost.mockResolvedValue(ok(useEnvelope(), 201));

    const result = await api.useWorkOrderTemplate(7, { quantity_ordered: 5 });

    expect(result.work_order.id).toBe(501);
    expect(result.work_order.work_order_number).toBe('WO-20260825-002');
    expect(result.work_order.status).toBe('draft');
    // The server DERIVED 63 from the copied nests' runs; 5 was only requested.
    expect(result.work_order.quantity_ordered).toBe(63);
  });

  it('preserves both skip lists verbatim, with the fields the UI names them by', async () => {
    const skippedOperation = {
      source_operation_id: 71,
      operation_number: 'Nest 3',
      sequence: 30,
      reason: 'laser_nest_deleted',
    };
    const skippedTie = {
      source_allocation_id: 9,
      part_id: 55,
      source_work_order_operation_id: 71,
      reason: 'part_not_available',
    };
    mockPost.mockResolvedValue(
      ok(useEnvelope({ skipped_operations: [skippedOperation], skipped_material_allocations: [skippedTie] }), 201)
    );

    const result = await api.useWorkOrderTemplate(7);

    expect(result.skipped_operations).toEqual([skippedOperation]);
    expect(result.skipped_material_allocations).toEqual([skippedTie]);
  });
});
