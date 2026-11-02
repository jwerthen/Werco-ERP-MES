import api from '../../services/api';
import {
  createDraftWO,
  matchEngineering,
  saveBOM,
  saveParts,
  saveRouting,
  type BOMRecord,
  type RoutingRecord,
} from './entry';
import { reviewedPlan, savedPart } from '../../test-utils/hankEngineering';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: Object.fromEntries(
    [
      'getParts',
      'getPart',
      'createPart',
      'createMaterial',
      'getBOMs',
      'getBOM',
      'createBOM',
      'getRoutings',
      'getRouting',
      'createRouting',
      'addRoutingOperation',
      'getWorkCenters',
      'getWorkOrders',
      'getWorkOrder',
      'createWorkOrder',
    ].map(name => [name, jest.fn()])
  ),
}));
const mock = jest.mocked(api);
const guard = jest.fn();
const record = jest.fn();
const p = reviewedPlan();
const bom = (): BOMRecord => ({
  id: 10,
  part_id: 1,
  revision: 'B',
  status: 'released',
  is_active: true,
  items: [
    {
      component_part_id: 2,
      quantity: 2,
      unit_of_measure: 'each',
      item_type: 'make',
      line_type: 'component',
      is_optional: false,
      is_alternate: false,
      scrap_factor: 0,
    },
  ],
});
const routing = (): RoutingRecord => ({
  id: 20,
  part_id: 1,
  revision: 'B',
  status: 'released',
  is_active: true,
  operations: [
    {
      id: 30,
      sequence: 10,
      name: 'TEST-DETAIL - Cut and deburr',
      work_center_id: 1,
      setup_hours: 0.2,
      run_hours_per_unit: 0.05,
      work_instructions: 'DEBURR ALL EDGES\nSource: synthetic.pdf, page 1; drawing TEST-ASM Rev B',
      is_active: true,
      is_inspection_point: false,
      is_outside_operation: false,
    },
  ],
});
beforeEach(() => {
  jest.clearAllMocks();
  mock.getParts.mockResolvedValue([savedPart(p, 0), savedPart(p, 1)]);
  mock.getBOMs.mockResolvedValue([]);
  mock.getRoutings.mockResolvedValue([]);
  mock.getWorkCenters.mockResolvedValue([
    { id: 1, is_active: true } as Awaited<ReturnType<typeof api.getWorkCenters>>[number],
  ]);
});
function releasedEngineering() {
  mock.getBOMs.mockResolvedValue([{ id: 10, part_id: 1 }]);
  mock.getBOM.mockResolvedValue(bom());
  mock.getRoutings.mockImplementation(async params => (params?.part_id === 1 ? [{ id: 20 }] : []));
  mock.getRouting.mockResolvedValue(routing());
}
test('matches before creating, and never creates an existing PN', async () => {
  await saveParts(p, guard, record);
  expect(mock.createPart).not.toHaveBeenCalled();
  expect(record).toHaveBeenCalledTimes(2);
});
test('conflicting part blocks the entire preflight before any write', async () => {
  mock.getParts.mockResolvedValue([{ ...savedPart(p, 0), revision: 'C' }]);
  await expect(saveParts(p, guard, record)).rejects.toThrow(/conflicts/);
  expect(mock.createPart).not.toHaveBeenCalled();
});
test('creates missing parts with source evidence, never pricing or stock fields', async () => {
  mock.getParts.mockResolvedValue([]);
  mock.createPart.mockImplementation(async data => savedPart(p, data.part_number === p.owner ? 0 : 1));
  mock.getPart.mockImplementation(async id => savedPart(p, id - 1));
  await saveParts(p, guard, record);
  expect(mock.createPart).toHaveBeenCalledTimes(2);
  const data = mock.createPart.mock.calls[0][0];
  expect(data.description).toContain('HANK-DRAWING:');
  expect(data).not.toHaveProperty('standard_cost');
  expect(data).not.toHaveProperty('quantity');
});
test('BOM creation preserves quantities per parent and is read back', async () => {
  mock.createBOM.mockResolvedValue({ ...bom(), status: 'draft' });
  mock.getBOM.mockResolvedValue({ ...bom(), status: 'draft' });
  await saveBOM(p, guard, record);
  expect(mock.createBOM.mock.calls[0][0].items[0].quantity).toBe(2);
  expect(mock.getBOM).toHaveBeenCalledWith(10);
});
test('released BOM mismatch is refused without edits', async () => {
  releasedEngineering();
  const changed = bom();
  changed.items[0].quantity = 3;
  mock.getBOM.mockResolvedValue(changed);
  await expect(saveBOM(p, guard, record)).rejects.toThrow(/Existing BOM differs/);
  expect(mock.createBOM).not.toHaveBeenCalled();
});
test('routing creates only reviewed operations and records header for partial recovery', async () => {
  mock.createRouting.mockResolvedValue({ ...routing(), operations: [], status: 'draft' });
  mock.getRouting.mockResolvedValue({ ...routing(), status: 'draft' });
  mock.addRoutingOperation.mockResolvedValue({});
  await saveRouting(p, guard, record);
  expect(mock.addRoutingOperation).toHaveBeenCalledWith(
    20,
    expect.objectContaining({ name: 'TEST-DETAIL - Cut and deburr', setup_hours: 0.2, run_hours_per_unit: 0.05 })
  );
  expect(record).toHaveBeenCalledWith(
    expect.objectContaining({ label: expect.stringContaining('operations pending') })
  );
});
test('uncertain routing save reports the created header, stops, and never releases', async () => {
  mock.createRouting.mockResolvedValue({ ...routing(), operations: [], status: 'draft' });
  mock.addRoutingOperation.mockRejectedValue(new Error('connection lost'));
  await expect(saveRouting(p, guard, record)).rejects.toThrow('connection lost');
  expect(record).toHaveBeenCalledTimes(1);
});
test('requires explicit WO request and released engineering', async () => {
  await expect(createDraftWO({ ...p, requestedWO: false }, guard, record)).rejects.toThrow(/explicitly requested/);
  await expect(createDraftWO(p, guard, record)).rejects.toThrow(/release the exact/);
  expect(mock.createWorkOrder).not.toHaveBeenCalled();
});
test('creates Draft WO from released engineering and verifies expanded quantity 2 × 4 = 8', async () => {
  releasedEngineering();
  mock.getWorkOrders.mockResolvedValue([]);
  const wo = {
    id: 40,
    work_order_number: 'WO-ACTUAL',
    status: 'draft',
    part_id: 1,
    quantity_ordered: 4,
    operations: [
      {
        sequence: 10,
        name: 'TEST-DETAIL - Cut and deburr',
        work_center_id: 1,
        component_part_id: 2,
        component_quantity: 8,
        run_instructions: 'DEBURR ALL EDGES\nSource: synthetic.pdf, page 1; drawing TEST-ASM Rev B',
        requires_inspection: false,
        setup_time_hours: 0.2,
        run_time_hours: 0.2,
      },
    ],
  };
  mock.createWorkOrder.mockResolvedValue(wo);
  mock.getWorkOrder.mockResolvedValue(wo);
  await createDraftWO(p, guard, record);
  expect(mock.createWorkOrder.mock.calls[0][0]).not.toHaveProperty('status');
  expect(mock.createWorkOrder.mock.calls[0][0]).not.toHaveProperty('operations');
  expect(record).toHaveBeenCalledWith(expect.objectContaining({ label: 'WO-ACTUAL', status: 'draft' }));
  mock.getWorkOrder.mockResolvedValue({ ...wo, operations: [{ ...wo.operations[0], component_quantity: 4 }] });
  await expect(createDraftWO(p, guard, record)).rejects.toThrow(/displayed quantity differs/);
});
test('does not duplicate an observed work order after an uncertain save', async () => {
  releasedEngineering();
  mock.getWorkOrders.mockResolvedValue([{ id: 40, part_id: 1 }]);
  mock.getWorkOrder.mockResolvedValue({
    id: 40,
    part_id: 1,
    work_order_number: 'WO-EXISTING',
    status: 'draft',
    notes: `HANK-DRAWING:${p.sha256}`,
  });
  await expect(createDraftWO(p, guard, record)).rejects.toThrow(/already references/);
  expect(mock.createWorkOrder).not.toHaveBeenCalled();
});
test('company/session changes stop before the next mutation', async () => {
  let calls = 0;
  const changed = () => {
    if (++calls > 1) throw new Error('Session changed');
  };
  await expect(saveParts(p, changed, record)).rejects.toThrow('Session changed');
  expect(mock.createPart).not.toHaveBeenCalled();
});
test('read failures are not interpreted as no matching records', async () => {
  mock.getParts.mockRejectedValue(new Error('network error'));
  await expect(matchEngineering(p, guard)).rejects.toThrow('network error');
  expect(mock.createPart).not.toHaveBeenCalled();
});
