import { blankOperation, extractDrawing, type DrawingPlan } from '../features/hank-engineering/model';
import type { Part } from '../types';
export function reviewedPlan(): DrawingPlan {
  const p = extractDrawing('synthetic.pdf', 'a'.repeat(64), [
    'DRAWING NUMBER: TEST-ASM\nPART NUMBER: TEST-ASM\nREV: B\nTITLE: Test assembly\nPART NUMBER | QTY | UOM | DESCRIPTION\nTEST-DETAIL | 2 | each | Test detail\nDEBURR ALL EDGES',
  ]);
  p.parts.forEach(part =>
    Object.assign(part, {
      revision: 'B',
      type: part.pn === 'TEST-ASM' ? 'assembly' : 'manufactured',
      uom: 'each',
      confirmed: true,
    })
  );
  p.components.forEach(line => {
    line.confirmed = true;
  });
  p.requirements.forEach(req => {
    req.confirmed = true;
  });
  p.operations = [
    {
      ...blankOperation(10),
      component: 'TEST-DETAIL',
      name: 'Cut and deburr',
      center: '1',
      setup: '12',
      run: '3',
      instructions: 'DEBURR ALL EDGES',
      note: 'Drawing page 1; Jon approved sequence, center and time standards.',
      confirmed: true,
    },
  ];
  return {
    ...p,
    screened: true,
    reviewed: true,
    reviewNotes: 'All sheets checked. No unresolved assumptions. Jon authorized engineering entry.',
    requestedWO: true,
    quantity: '4',
    quantitySource: 'Jon requested four assemblies and a Draft WO.',
  };
}
export function savedPart(plan: DrawingPlan, index: number): Part {
  const p = plan.parts[index];
  return {
    id: index + 1,
    version: 1,
    part_number: p.pn,
    revision: p.revision,
    name: p.name,
    part_type: p.type as Part['part_type'],
    unit_of_measure: p.uom,
    drawing_number: p.pn === plan.owner ? plan.number : undefined,
    customer_name: p.pn === plan.owner ? plan.customer : undefined,
    standard_cost: 0,
    is_critical: false,
    requires_inspection: true,
    backflush_components: false,
    is_active: true,
    status: 'active',
    created_at: '',
    updated_at: '',
  };
}
