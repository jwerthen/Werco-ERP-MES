import { newId } from './types';
import type { OperationLine, ProcessProfile } from './types';

/** A library revision provides candidate process data, never approval or a new part's geometry. */
export function operationFromProfile(profile: ProcessProfile, partId: string, currency: string): OperationLine {
  if (profile.currency !== currency) throw new Error(`This profile is priced in ${profile.currency}; the quote uses ${currency}. No currency conversion is implied.`);
  if (!partId) throw new Error('Choose the part receiving this operation.');
  const op: OperationLine = JSON.parse(JSON.stringify(profile.template));
  op.id = newId('op'); op.part_id = partId;
  op.evidence = { reviewed: false, status: 'assumption', source: `process-profile:${profile.key}:r${profile.revision}`, note: `Copied ${profile.name}, revision ${profile.revision}. ${profile.evidence_note}\nOriginal source: ${profile.template.evidence.source || 'Unspecified'}. ${profile.template.evidence.note || ''}\nReview machine, material, thickness, tooling and rates for this part. Part geometry and attended run allowances were cleared.` };
  op.consumables_cost_per_run = null; op.outside_cost_per_run = null;
  const r = op.recipe;
  if (r.kind === 'manual') { r.labor_seconds = null; r.machine_seconds = null; }
  if (r.kind === 'laser') { r.cuts = r.cuts.map(c => ({ ...c, cut_length_mm: '', pierces: NaN })); r.noncut_machine_seconds = null; r.labor_seconds = null; r.dynamics_allowance_seconds = null; }
  if (r.kind === 'brake') { r.hits = NaN; r.handling_seconds = null; r.inspection_seconds = null; r.machine_seconds = null; r.feasibility_reviewed = false; }
  if (r.kind === 'weld') { r.weld_length_mm = null; r.weld_size_mm = null; r.nonweld_labor_seconds = null; r.nonweld_machine_seconds = null; r.procedure_reference = null; }
  return op;
}
