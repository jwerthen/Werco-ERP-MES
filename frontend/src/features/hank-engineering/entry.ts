/** Native signed-in screen actions. Never registered as Hank/model tools. */
import api from '../../services/api';
import type { Part } from '../../types';
import { issues, key, matchPart, type DrawingPlan } from './model';

export interface Receipt {
  kind: 'part' | 'bom' | 'routing' | 'work_order';
  id: number;
  label: string;
  href: string;
  action: 'created' | 'matched';
  status?: string;
  details?: string[];
}
export interface BOMRecord {
  id: number;
  part_id: number;
  revision: string;
  status: string;
  is_active: boolean;
  items: {
    component_part_id: number;
    quantity: number;
    unit_of_measure: string;
    item_type: string;
    line_type: string;
    is_optional: boolean;
    is_alternate: boolean;
    scrap_factor: number;
  }[];
}
export interface RoutingRecord {
  id: number;
  part_id: number;
  revision: string;
  status: string;
  is_active: boolean;
  operations: {
    id: number;
    sequence: number;
    name: string;
    description?: string;
    work_center_id: number;
    setup_hours: number;
    run_hours_per_unit: number;
    work_instructions?: string;
    is_active: boolean;
    is_inspection_point: boolean;
    is_outside_operation: boolean;
    process_sheet_id?: number | null;
    work_center?: { code: string; name: string };
  }[];
}
export interface Snapshot {
  parts: Map<string, Part>;
  bom?: BOMRecord;
  routing?: RoutingRecord;
}
export type Guard = () => void;
export type RecordReceipt = (receipt: Receipt) => void;
const marker = (p: DrawingPlan) => `HANK-DRAWING:${p.sha256}`;
const source = (p: DrawingPlan) =>
  `${marker(p)}\nDrawing ${p.number} Rev ${p.revision}; ${p.filename}\nReview: ${p.reviewNotes}`;
const instruction = (p: DrawingPlan, note: string, page: number) =>
  `${note}\nSource: ${p.filename}, page ${page}; drawing ${p.number} Rev ${p.revision}`;
const title = (op: DrawingPlan['operations'][number]) => `${key(op.component)} - ${op.name.trim()}`;

function requirePlan(plan: DrawingPlan) {
  const errors = issues(plan);
  if (errors.length) throw new Error(errors.join(' '));
  if (source(plan).length > 1800) throw new Error('Shorten review notes to fit the engineering record.');
}

export async function matchEngineering(plan: DrawingPlan, guard: Guard): Promise<Snapshot> {
  requirePlan(plan);
  guard();
  const records = await api.getParts({ active_only: false, item_group: 'all', include_bom_components: true });
  guard();
  const parts = new Map<string, Part>();
  for (const part of plan.parts) {
    const found = matchPart(part, records, plan);
    if (found) parts.set(key(part.pn), found);
  }
  const owner = parts.get(key(plan.owner));
  let bom: BOMRecord | undefined;
  let routing: RoutingRecord | undefined;
  if (owner) {
    guard();
    const boms: { id: number; part_id: number }[] = await api.getBOMs({ active_only: false });
    guard();
    if (
      plan.requestedWO &&
      boms.some(row =>
        plan.components.some(line => {
          const component = parts.get(key(line.pn));
          return component && component.id === row.part_id && ['manufactured', 'assembly'].includes(component.part_type);
        })
      )
    )
      throw new Error('A component has its own BOM. Review nested Engineering before requesting a Draft WO.');
    const candidates = boms.filter(row => row.part_id === owner.id);
    if (candidates.length > 1) throw new Error('Multiple BOMs match the owner. Review existing Engineering.');
    if (candidates[0]) {
      bom = await api.getBOM(candidates[0].id);
      guard();
    }
    const routings: { id: number }[] = await api.getRoutings({ part_id: owner.id, active_only: false });
    guard();
    if (routings.length > 1)
      throw new Error('Multiple routing revisions exist. Resolve revision selection in Routing first.');
    if (routings[0]) {
      routing = await api.getRouting(routings[0].id);
      guard();
    }
  }
  const snapshot = { parts, bom, routing };
  if (bom) verifyBOM(plan, snapshot, bom);
  if (routing) verifyRouting(plan, routing);
  guard();
  const centers = await api.getWorkCenters();
  guard();
  if (plan.operations.some(op => !centers.some(center => center.id === Number(op.center) && center.is_active)))
    throw new Error('A reviewed work center is no longer active. Review the routing again.');
  const assembly = plan.parts.find(part => key(part.pn) === key(plan.owner))?.type === 'assembly';
  if (plan.requestedWO && (assembly || plan.components.length > 0 || bom)) {
    // The native assembly generator renumbers operations and moves inspections
    // last. Mirror its classification before any WO write, including descriptions
    // on matched routings and the currently observed work center.
    if (plan.operations.some((op, index) => op.sequence !== (index + 1) * 10))
      throw new Error('Assembly Work Orders require consecutive reviewed sequences 10, 20, 30.');
    let inspectionSeen = false;
    for (const op of plan.operations) {
      const center = centers.find(row => row.id === Number(op.center));
      const saved = routing?.operations.find(row => row.is_active && row.sequence === op.sequence);
      const inspection =
        op.inspection ||
        [title(op), saved?.description || '', center?.name || '', center?.work_center_type || ''].some(value =>
          /INSPECT|INSPECTION|QUALITY|QC/i.test(value)
        );
      if (!inspection && inspectionSeen)
        throw new Error(
          'The native assembly Work Order would move an inspection after a later manufacturing step. Review process order in Engineering before creating the job.'
        );
      inspectionSeen = inspectionSeen || inspection;
    }
  }
  return snapshot;
}

function verifyBOM(plan: DrawingPlan, snapshot: Snapshot, bom: BOMRecord) {
  if (
    !bom.is_active ||
    key(bom.revision) !== key(plan.revision) ||
    !['draft', 'released'].includes(bom.status) ||
    bom.items.length !== plan.components.length ||
    bom.items.some(line => {
      const wanted = plan.components.find(row => snapshot.parts.get(key(row.pn))?.id === line.component_part_id);
      const part = plan.parts.find(row => key(row.pn) === key(wanted?.pn || ''));
      return (
        !wanted ||
        !part ||
        Number(line.quantity) !== Number(wanted.quantity) ||
        line.unit_of_measure !== wanted.uom ||
        line.item_type !== (['manufactured', 'assembly'].includes(part.type) ? 'make' : 'buy') ||
        line.line_type !== 'component' ||
        line.is_optional ||
        line.is_alternate ||
        Number(line.scrap_factor) !== 0
      );
    })
  )
    throw new Error(
      'Existing BOM differs from the reviewed components/revision. Use BOM revision controls; nothing was overwritten.'
    );
}
function verifyRouting(plan: DrawingPlan, routing: RoutingRecord) {
  const ops = routing.operations.filter(op => op.is_active);
  if (
    !routing.is_active ||
    key(routing.revision) !== key(plan.revision) ||
    !['draft', 'released'].includes(routing.status) ||
    ops.length !== plan.operations.length ||
    ops.some(op => {
      const wanted = plan.operations.find(row => row.sequence === op.sequence);
      return (
        !wanted ||
        op.name !== title(wanted) ||
        op.work_center_id !== Number(wanted.center) ||
        Number(op.setup_hours) !== Number(wanted.setup) / 60 ||
        Number(op.run_hours_per_unit) !== Number(wanted.run) / 60 ||
        (op.work_instructions || '') !== instruction(plan, wanted.instructions, wanted.page) ||
        op.is_inspection_point !== wanted.inspection ||
        op.is_outside_operation ||
        op.process_sheet_id
      );
    })
  )
    throw new Error(
      'Existing routing differs from the reviewed operations/revision. Use Routing revision controls; nothing was overwritten.'
    );
}
function requireParts(plan: DrawingPlan, snapshot: Snapshot) {
  if (snapshot.parts.size !== plan.parts.length) throw new Error('Match or create every reviewed part first.');
  return snapshot.parts.get(key(plan.owner))!;
}
const partReceipt = (part: Part, action: Receipt['action']): Receipt => ({
  kind: 'part',
  id: part.id,
  label: part.part_number,
  href: `/parts/${part.id}`,
  action,
});

export async function saveParts(plan: DrawingPlan, guard: Guard, record: RecordReceipt) {
  const snapshot = await matchEngineering(plan, guard);
  for (const part of plan.parts) {
    guard();
    const existing = snapshot.parts.get(key(part.pn));
    if (existing) {
      record(partReceipt(existing, 'matched'));
      continue;
    }
    const payload = {
      part_number: key(part.pn),
      revision: key(part.revision),
      name: part.name.trim(),
      part_type: part.type,
      unit_of_measure: part.uom,
      description: `${part.description}\n${source(plan)}\nPage ${part.page}: ${part.note}`,
      ...(key(part.pn) === key(plan.owner) ? { drawing_number: plan.number, customer_name: plan.customer } : {}),
    };
    if (payload.description.length > 2000)
      throw new Error(`${part.pn}: shorten description/evidence to fit the record.`);
    // The normal Parts / Materials authoring endpoints retain role, tenant and audit rules.
    const created = await (['manufactured', 'assembly'].includes(part.type)
      ? api.createPart(payload)
      : api.createMaterial(payload));
    guard();
    record(partReceipt(created, 'created'));
    const verified = await api.getPart(created.id);
    guard();
    matchPart(part, [verified], plan);
  }
}

export async function saveBOM(plan: DrawingPlan, guard: Guard, record: RecordReceipt) {
  const snapshot = await matchEngineering(plan, guard);
  const owner = requireParts(plan, snapshot);
  if (!plan.components.length) throw new Error('This drawing has no reviewed BOM components.');
  let bom = snapshot.bom;
  const action = bom ? 'matched' : 'created';
  if (!bom) {
    guard();
    bom = await api.createBOM({
      part_id: owner.id,
      revision: key(plan.revision),
      description: source(plan),
      items: plan.components.map((line, i) => {
        const part = snapshot.parts.get(key(line.pn))!;
        return {
          component_part_id: part.id,
          item_number: (i + 1) * 10,
          quantity: Number(line.quantity),
          unit_of_measure: line.uom,
          item_type: ['manufactured', 'assembly'].includes(part.part_type) ? 'make' : 'buy',
          line_type: 'component',
          notes: instruction(plan, line.note, line.page),
        };
      }),
    });
    guard();
  }
  if (!bom) throw new Error('No BOM receipt was returned.');
  record({
    kind: 'bom',
    id: bom.id,
    label: `${plan.owner} BOM`,
    href: `/bom?id=${bom.id}`,
    action,
    status: bom.status,
  });
  const saved: BOMRecord = await api.getBOM(bom.id);
  guard();
  verifyBOM(plan, snapshot, saved);
}

export async function saveRouting(plan: DrawingPlan, guard: Guard, record: RecordReceipt) {
  const snapshot = await matchEngineering(plan, guard);
  const owner = requireParts(plan, snapshot);
  let routing = snapshot.routing;
  const action = routing ? 'matched' : 'created';
  if (!routing) {
    guard();
    routing = await api.createRouting({ part_id: owner.id, revision: key(plan.revision), description: source(plan) });
    guard();
    if (!routing) throw new Error('No routing receipt was returned.');
    record({
      kind: 'routing',
      id: routing.id,
      label: `${plan.owner} routing (operations pending)`,
      href: `/routing?id=${routing.id}`,
      action,
      status: routing.status,
    });
    for (const op of plan.operations) {
      guard();
      await api.addRoutingOperation(routing.id, {
        sequence: op.sequence,
        name: title(op),
        work_center_id: Number(op.center),
        setup_hours: Number(op.setup) / 60,
        run_hours_per_unit: Number(op.run) / 60,
        work_instructions: instruction(plan, op.instructions, op.page),
        is_inspection_point: op.inspection,
      });
      guard();
    }
  }
  const saved: RoutingRecord = await api.getRouting(routing.id);
  guard();
  verifyRouting(plan, saved);
  record({
    kind: 'routing',
    id: saved.id,
    label: `${plan.owner} routing`,
    href: `/routing?id=${saved.id}`,
    action,
    status: saved.status,
    details: saved.operations
      .filter(op => op.is_active)
      .map(
        op =>
          `${op.sequence}: ${op.name}; ${op.work_center ? `${op.work_center.code} - ${op.work_center.name}` : `work center ID ${op.work_center_id}`}`
      ),
  });
}

interface WOOperation {
  name: string;
  sequence: number;
  work_center_id: number;
  component_quantity?: number;
  component_part_id?: number;
  status?: string;
  run_instructions?: string;
  requires_inspection?: boolean;
  setup_time_hours?: number;
  run_time_hours?: number;
}
interface WO {
  id: number;
  work_order_number: string;
  part_id: number;
  status: string;
  quantity_ordered: number;
  notes?: string;
  operations: WOOperation[];
}
function verifyOperations(plan: DrawingPlan, operations: WOOperation[], snapshot: Snapshot) {
  if (operations.length !== plan.operations.length)
    throw new Error('Operation count differs from the reviewed routing. Review Engineering before continuing.');
  for (const wanted of plan.operations) {
    const op = operations.find(row => row.sequence === wanted.sequence);
    const line = plan.components.find(row => key(row.pn) === key(wanted.component));
    const expected = Number(plan.quantity) * (line ? Number(line.quantity) : 1);
    if (
      !op ||
      op.name !== title(wanted) ||
      op.work_center_id !== Number(wanted.center) ||
      op.run_instructions !== instruction(plan, wanted.instructions, wanted.page) ||
      op.requires_inspection !== wanted.inspection ||
      Math.abs(Number(op.setup_time_hours) - Number(wanted.setup) / 60) > 0.00001 ||
      !Number.isFinite(Number(op.setup_time_hours)) ||
      Math.abs(Number(op.run_time_hours) - (Number(wanted.run) / 60) * Number(plan.quantity)) > 0.00001 ||
      !Number.isFinite(Number(op.run_time_hours)) ||
      Number(op.component_quantity ?? plan.quantity) !== expected ||
      (line && op.component_part_id !== snapshot.parts.get(key(line.pn))?.id)
    )
      throw new Error(
        `Operation ${wanted.sequence}: part, instructions, time, inspection, center or displayed quantity differs from the reviewed drawing. Stop for Engineering review.`
      );
  }
}
export async function createDraftWO(plan: DrawingPlan, guard: Guard, record: RecordReceipt) {
  if (!plan.requestedWO) throw new Error('A new Work Order must be explicitly requested.');
  const snapshot = await matchEngineering(plan, guard);
  const owner = requireParts(plan, snapshot);
  if (
    !snapshot.routing ||
    snapshot.routing.status !== 'released' ||
    (plan.components.length && snapshot.bom?.status !== 'released')
  )
    throw new Error('Review and release the exact BOM/routing in their existing screens before creating a Draft WO.');
  guard();
  // The list response is a summary and intentionally has no notes. Read each
  // candidate detail before deciding the source marker is absent.
  const existing: { id: number; part_id: number }[] = await api.getWorkOrders({ search: owner.part_number });
  guard();
  for (const candidate of existing.filter(row => row.part_id === owner.id)) {
    const observed: WO = await api.getWorkOrder(candidate.id);
    guard();
    if (observed.notes?.includes(marker(plan))) {
      record({
        kind: 'work_order',
        id: observed.id,
        label: observed.work_order_number,
        href: `/work-orders/${observed.id}`,
        action: 'matched',
        status: observed.status,
      });
      throw new Error(
        'A Work Order already references this drawing intake. Open its receipt and verify it before requesting another job.'
      );
    }
  }
  // Component routings would be expanded in addition to the owner's routing.
  // Refuse that surprise before creating a job; do not suppress/override operations.
  for (const line of plan.components) {
    guard();
    const childRoutings: { status: string }[] = await api.getRoutings({
      part_id: snapshot.parts.get(key(line.pn))!.id,
    });
    guard();
    if (childRoutings.some(row => row.status === 'released'))
      throw new Error(`${line.pn}: a component routing would add operations. Review ownership in Engineering first.`);
  }
  guard();
  const created: WO = await api.createWorkOrder({
    part_id: owner.id,
    quantity_ordered: Number(plan.quantity),
    customer_name: plan.customer || undefined,
    notes: `${source(plan)}\nJob quantity authorized: ${plan.quantitySource}`,
  });
  guard();
  record({
    kind: 'work_order',
    id: created.id,
    label: created.work_order_number,
    href: `/work-orders/${created.id}`,
    action: 'created',
    status: created.status,
  });
  const saved: WO = await api.getWorkOrder(created.id);
  guard();
  if (
    saved.status !== 'draft' ||
    saved.part_id !== owner.id ||
    Number(saved.quantity_ordered) !== Number(plan.quantity)
  )
    throw new Error('Saved Work Order does not match the requested Draft job. Stop and inspect the recorded link.');
  verifyOperations(plan, saved.operations, snapshot);
  record({
    kind: 'work_order',
    id: saved.id,
    label: saved.work_order_number,
    href: `/work-orders/${saved.id}`,
    action: 'created',
    status: saved.status,
    details: [
      `Job quantity ${saved.quantity_ordered}; ${saved.operations.length} operations verified`,
      ...saved.operations.map(
        op => `${op.sequence}: ${op.name}; quantity ${op.component_quantity ?? saved.quantity_ordered}`
      ),
    ],
  });
}
