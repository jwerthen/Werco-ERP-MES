import { inputErrors, mayApprove, scenarioPlan } from './model';
import { emptyPlan, newEvidence, newRecipe } from './types';
import type { CalculationResult, OperationLine, ProcessProfile, QuoteRecord } from './types';
import { operationFromProfile } from './profiles';

const calculation: CalculationResult = { engine_version: 'test', input_hash: 'hash', can_approve: true, issues: [], totals: { total_cost: '1.00' } };
const record = (): QuoteRecord => ({ id: 1, title: 'Package', customer_id: null, revision: 2, status: 'draft', plan: emptyPlan(), calculation, files: [] });
const operation = (): OperationLine => ({ id: 'old-op', part_id: 'old-part', name: 'Brake', process: 'brake', setup_basis: 'per_quote', run_basis: 'per_unit', batch_size: '1', setup_labor_seconds: '600', setup_machine_seconds: '600', labor_rate_per_hour: '75.125', machine_rate_per_hour: null, consumables_cost_per_run: '2', outside_cost_per_run: '0', recipe: { kind: 'brake', hits: 5, seconds_per_hit: '4', handling_seconds: '20', inspection_seconds: '5', crew_size: 2, machine_seconds: '50', feasibility_reviewed: true }, evidence: { reviewed: true, status: 'validated', source: 'previous-drawing', note: 'Fixture validation' } });

test('approval requires saved current inputs, no blocking issues and exact source review hashes', () => {
  const r = record(); expect(mayApprove(r, false, false, calculation, true)).toBe(true);
  expect(mayApprove(r, true, false, calculation, true)).toBe(false);
  expect(mayApprove(r, false, true, calculation, true)).toBe(false);
  expect(mayApprove(r, false, false, calculation, false)).toBe(false);
  expect(mayApprove(r, false, false, { ...calculation, issues: [{ severity: 'blocking', code: 'missing', path: '', message: 'Missing' }] }, true)).toBe(false);
  r.files = [{ id: 9, sha256: 'a'.repeat(64), file_name: 'drawing.pdf' }];
  r.plan.source_reviews = [{ file_id: 9, sha256: 'b'.repeat(64), disposition: 'reviewed', note: 'Reviewed drawing' }];
  expect(mayApprove(r, false, false, calculation, true)).toBe(false);
  r.plan.source_reviews[0].sha256 = r.files[0].sha256;
  expect(mayApprove(r, false, false, calculation, true)).toBe(true);
  r.status = 'approved'; expect(mayApprove(r, false, false, calculation, true)).toBe(false);
});

test('unknown values remain null and malformed numbers cannot silently become zero', () => {
  const plan = emptyPlan(); plan.parts.push({ id: 'part', name: 'Part', make_or_buy: 'buy', purchase_unit_cost: null, costing_complete: false, evidence: newEvidence() });
  expect(inputErrors({ title: 'Test', customer_id: null, plan })).toEqual([]);
  plan.parts[0].purchase_unit_cost = '0.123456789'; expect(inputErrors({ title: 'Test', customer_id: null, plan })).toEqual([]);
  for (const invalid of ['', '1e3', '-1', 'abc', '0.1234567890']) { plan.parts[0].purchase_unit_cost = invalid; expect(inputErrors({ title: 'Test', customer_id: null, plan }).length).toBeGreaterThan(0); }
  plan.parts[0].purchase_unit_cost = null; plan.operations.push({ ...operation(), recipe: { ...newRecipe('brake'), hits: NaN } as OperationLine['recipe'] });
  expect(inputErrors({ title: 'Test', customer_id: null, plan }).join()).toContain('whole number');
});

test('quantity scenarios preserve source and BOM context without changing the original demand', () => {
  const plan = emptyPlan(); plan.roots = [{ part_id: 'assembly', quantity: '2' }]; plan.bom = [{ id: 'edge', parent_id: 'assembly', child_id: 'part', quantity: '3' }];
  const scenario = scenarioPlan(plan, '250.000000001'); expect(scenario.roots[0].quantity).toBe('250.000000001'); expect(plan.roots[0].quantity).toBe('2'); expect(scenario.bom).toEqual(plan.bom);
  expect(() => scenarioPlan(plan, '0')).toThrow(); expect(() => scenarioPlan({ ...plan, roots: [] }, '5')).toThrow();
});

test('profile application keeps exact candidate rates and provenance but clears geometry and approval', () => {
  const profile: ProcessProfile = { id: 1, key: 'profile-id', revision: 3, name: 'Ermaksan brake', process: 'brake', currency: 'USD', machine: null, material: null, thickness_mm: null, evidence_note: 'Measured shop trial', template: operation(), created_at: '', created_by: 1 };
  const copied = operationFromProfile(profile, 'new-part', 'USD');
  expect(copied.id).not.toBe(profile.template.id); expect(copied.part_id).toBe('new-part'); expect(copied.labor_rate_per_hour).toBe('75.125'); expect(copied.evidence.reviewed).toBe(false); expect(copied.evidence.source).toContain('profile-id:r3');
  expect(copied.recipe).toMatchObject({ kind: 'brake', hits: NaN, feasibility_reviewed: false, machine_seconds: null }); expect(copied.consumables_cost_per_run).toBeNull(); expect(profile.template.evidence.reviewed).toBe(true);
  expect(() => operationFromProfile(profile, 'part', 'CAD')).toThrow('currency');
});
