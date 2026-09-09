import { z } from 'zod';
import { canonicalJSON, sha256 } from './provenance';
import { canonicalInches, stockPieceEvidenceSchema } from '../../../validation/stockPiece';
import {
  REMNANT_PLANNING_ADVISORY,
  type RemnantPlan,
  type RemnantResolution,
  type RemnantSnapshot,
  type RemnantSnapshotRequest,
} from '../../../types/remnantPlanning';
import { REMNANT_DOMAIN_PROFILE, requireRemnantDomainProfile } from './remnant-domain-profile';
import { policyThicknessIn } from './spacing-policy';
import { quoteFromFile } from './quoting';
import { requireCurrentGeometryProfile } from './geometry-profile';

export const REMNANT_EVIDENCE_PREFIX = 'werco-remnant-evidence-v1\n';

/** Python ensure_ascii=True compatible spelling, including surrogate pairs and DEL. */
function asciiString(value: string): string {
  return JSON.stringify(value).replace(
    /[\u007f-\uffff]/g,
    code => `\\u${code.charCodeAt(0).toString(16).padStart(4, '0')}`
  );
}

/** Dedicated evidence encoding. Never replace any historical JSON/profile canonicalizer. */
export function canonicalRemnantEvidence(value: unknown): string {
  const active = new Set<object>();
  const pieces = [REMNANT_EVIDENCE_PREFIX];
  let size = REMNANT_EVIDENCE_PREFIX.length,
    nodes = 0;
  const emit = (text: string) => {
    size += text.length;
    if (size > 32 * 1024 * 1024) throw new Error('Remnant canonical evidence exceeds its byte budget.');
    pieces.push(text);
  };
  const encode = (item: unknown, depth: number): void => {
    nodes++;
    if (depth > 32 || nodes > 1_000_000) throw new Error('Remnant evidence exceeds its structural budget.');
    if (item === null) {
      emit('n');
      return;
    }
    if (typeof item === 'boolean') {
      emit(item ? 'b1' : 'b0');
      return;
    }
    if (typeof item === 'string') {
      emit(`s${asciiString(item)}`);
      return;
    }
    if (typeof item === 'number') {
      if (!Number.isFinite(item) || (Number.isInteger(item) && !Number.isSafeInteger(item)))
        throw new Error('Remnant evidence numbers must be finite and integer values must be safe.');
      const bytes = new Uint8Array(8);
      new DataView(bytes.buffer).setFloat64(0, Object.is(item, -0) ? 0 : item, false);
      emit(`d${Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')}`);
      return;
    }
    if (
      !item ||
      typeof item !== 'object' ||
      (!Array.isArray(item) && Object.getPrototypeOf(item) !== Object.prototype && Object.getPrototypeOf(item) !== null)
    )
      throw new Error('Remnant evidence must contain only JSON values.');
    if (active.has(item)) throw new Error('Remnant evidence cannot contain circular references.');
    if (Object.getOwnPropertySymbols(item).length)
      throw new Error('Remnant evidence cannot contain symbol properties.');
    const descriptors = Object.getOwnPropertyDescriptors(item);
    const keys = Object.keys(descriptors).filter(key => !(Array.isArray(item) && key === 'length'));
    if (
      keys.some(key => !descriptors[key].enumerable || !Object.prototype.hasOwnProperty.call(descriptors[key], 'value'))
    )
      throw new Error('Remnant evidence cannot contain hidden properties or accessors.');
    active.add(item);
    try {
      if (Array.isArray(item)) {
        if (keys.length !== item.length || keys.some((key, index) => key !== String(index)))
          throw new Error('Remnant evidence arrays must be dense and cannot have extra properties.');
        emit('a[');
        keys.forEach((key, index) => {
          if (index) emit(',');
          encode(descriptors[key].value, depth + 1);
        });
        emit(']');
      } else {
        const sorted = keys
          .map(key => ({ key, text: asciiString(key) }))
          .sort((a, b) => (a.text < b.text ? -1 : a.text > b.text ? 1 : 0));
        emit('o{');
        sorted.forEach(({ key, text }, index) => {
          if (index) emit(',');
          emit(text + ':');
          encode(descriptors[key].value, depth + 1);
        });
        emit('}');
      }
    } finally {
      active.delete(item);
    }
  };
  encode(value, 0);
  return pieces.join('');
}

export async function remnantEvidenceHash(value: unknown): Promise<string> {
  return sha256(canonicalRemnantEvidence(value));
}

/** quote is the exact saved imperial group object, before any mm conversion or round trip. */
export function targetGroupHash(input: { groupId: string; requiredGrade: string; quote: unknown }): Promise<string> {
  return remnantEvidenceHash(input);
}

const id = z.number().int().positive().max(2147483647);
const hash = z.string().regex(/^[a-f0-9]{64}$/);
const cleanText = (max: number) =>
  z
    .string()
    .min(1)
    .max(max)
    .refine(value => value.trim() === value);
const historicText = z.string().max(1024).nullable();
const utc = z
  .string()
  .refine(
    value =>
      /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$/.test(value) &&
      Number.isFinite(Date.parse(value)) &&
      new Date(value).toISOString().slice(0, 19) === value.slice(0, 19)
  );
const sourceSchema = z
  .object({
    version: z.literal(1),
    item: z
      .object({
        id,
        company_id: id,
        part_id: id,
        location: z.string().max(1024),
        warehouse: historicText,
        lot_number: historicText,
        serial_number: historicText,
        received_date: historicText,
        supplier_id: id.nullable(),
        po_number: historicText,
        cert_number: historicText,
        heat_lot: historicText,
        expiration_date: historicText,
        status: z.literal('available'),
        is_active: z.literal(true),
        updated_at: historicText,
      })
      .strict(),
    part: z
      .object({
        id,
        company_id: id,
        part_number: z.string().max(1024),
        revision: historicText,
        name: z.string().max(1024),
        part_type: historicText,
        unit_of_measure: historicText,
        is_active: z.literal(true),
        is_deleted: z.literal(false),
        updated_at: historicText,
      })
      .strict(),
    movement_watermark: z
      .object({
        coverage: z.literal('direct_item_and_unattributed_same_part'),
        count: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
        max_id: id.nullable(),
        max_created_at: historicText,
      })
      .strict(),
  })
  .strict();
const snapshotSchema = z
  .object({
    version: z.literal(1),
    companyId: id,
    pieceId: id,
    label: cleanText(120),
    observationNumber: id,
    state: z.literal('RECORDED'),
    observedAt: utc,
    observerName: cleanText(120),
    reason: cleanText(1000),
    createdAt: utc,
    createdBy: id,
    submittedApiTokenId: id.nullable(),
    payloadSchemaVersion: z.literal(1),
    payloadSha256: hash,
    payloadBytes: z.number().int().positive().max(131072),
    evidence: stockPieceEvidenceSchema,
    sourceInventoryItemId: id,
    sourcePartId: id,
    sourceSha256: hash,
    sourceEvidence: sourceSchema,
  })
  .strict();
const resolutionSchema = z
  .object({
    company_id: id,
    snapshot: snapshotSchema,
    snapshot_sha256: hash,
    latest_observation_number: id,
    source_status: z.literal('unchanged'),
    current_source_sha256: hash,
    checked_at: utc,
    review_issues: z.array(z.string().max(2000)).max(100),
    advisory: z.literal(REMNANT_PLANNING_ADVISORY),
  })
  .strict();
const assignmentSchema = z
  .object({
    version: z.literal(1),
    basis: z.literal('planner_declared_unverified'),
    family: z.enum(['Carbon steel', 'Stainless steel', 'Aluminum']),
    requiredGrade: cleanText(120),
    thicknessIn: z.string().max(24),
    reason: cleanText(1000),
    targetGroupSha256: hash,
  })
  .strict();
const planSchema = z
  .object({
    version: z.literal(1),
    groupId: z.string().min(1).max(200),
    snapshot: snapshotSchema,
    snapshotSha256: hash,
    assignment: assignmentSchema,
    geometryProfile: z.object({ id: z.string(), sha256: hash }).strict(),
    zoneClearanceIn: z.string().max(24),
    capacity: z.literal(1),
    planningOnly: z.literal(true),
    eligibilityVerified: z.literal(false),
    availabilityVerified: z.literal(false),
  })
  .strict();
function check(ok: unknown, message: string): asserts ok {
  if (!ok) throw new Error(message);
}
function unchangedJSON(source: unknown, parsed: unknown): void {
  check(
    canonicalRemnantEvidence(source) === canonicalRemnantEvidence(parsed),
    'Reported evidence must be canonical; it cannot be repaired during selection.'
  );
}

/** Validate evidence, not physical presence. Never accept canonicalizing repairs from form schemas. */
export async function validateRemnantSnapshot(value: unknown): Promise<RemnantSnapshot> {
  const snapshot = snapshotSchema.parse(value);
  unchangedJSON(value, snapshot);
  const { evidence, sourceEvidence: source } = snapshot;
  check(evidence.geometry.kind !== 'unknown', 'Record an actual outline before planning with this piece.');
  check(evidence.thickness !== null && evidence.grade !== null, 'Reported thickness and grade must be known.');
  check(Number(evidence.thickness) * 25.4 <= 100, 'Reported thickness exceeds the nesting limit.');
  check(
    source.item.id === snapshot.sourceInventoryItemId &&
      source.item.company_id === snapshot.companyId &&
      source.item.part_id === snapshot.sourcePartId &&
      source.part.id === snapshot.sourcePartId &&
      source.part.company_id === snapshot.companyId,
    'The recorded source identities do not match this observation.'
  );
  const bytes = canonicalJSON(evidence);
  check(
    new TextEncoder().encode(bytes).length === snapshot.payloadBytes &&
      (await sha256(bytes)) === snapshot.payloadSha256,
    'Reported evidence does not match its original payload fingerprint.'
  );
  check(new TextEncoder().encode(JSON.stringify(snapshot)).length <= 262144, 'The planning snapshot exceeds 256 KiB.');
  return snapshot;
}

/** Bind a fresh resolver response to the exact requested row; latestness cannot come from history alone. */
export async function validateRemnantResolution(
  value: unknown,
  expected: RemnantSnapshotRequest & { pieceId: number; observationNumber: number }
): Promise<RemnantResolution> {
  const result = resolutionSchema.parse(value);
  unchangedJSON(value, result);
  const snapshot = await validateRemnantSnapshot(result.snapshot);
  check(
    result.company_id === expected.expected_company_id &&
      snapshot.companyId === expected.expected_company_id &&
      snapshot.pieceId === expected.pieceId &&
      snapshot.observationNumber === expected.observationNumber &&
      snapshot.payloadSha256 === expected.expected_payload_sha256 &&
      snapshot.sourceSha256 === expected.expected_source_sha256 &&
      result.latest_observation_number === snapshot.observationNumber &&
      result.current_source_sha256 === snapshot.sourceSha256,
    'The current observation or source differs from the selected piece. Refresh and select it again.'
  );
  check(
    (await remnantEvidenceHash(snapshot)) === result.snapshot_sha256,
    'The planning snapshot fingerprint does not match.'
  );
  return result;
}

export function remnantTarget(quote: unknown): { family: string; thicknessIn: string; marginIn: number } {
  const source = z
    .object({
      version: z.literal(14),
      units: z.literal('in'),
      material: z.string(),
      thickness: z.number().positive().finite(),
      margin: z.number().nonnegative().finite(),
      parts: z.array(z.unknown()).min(1).max(300),
    })
    .passthrough()
    .parse(quote);
  requireCurrentGeometryProfile(source.geometryProfile);
  return { family: source.material, thicknessIn: policyThicknessIn(source.thickness), marginIn: source.margin };
}

export function normalizedZoneClearance(input: string): string {
  const result = canonicalInches(input);
  check(Number(result) >= 0 && Number(result) <= 100, 'Zone clearance must be between 0 and 100 inches.');
  return result;
}

export function defaultZoneClearance(quote: unknown): string {
  const margin = remnantTarget(quote).marginIn;
  // Existing floating file margins need a conservative bridge into the exact nanoinch field.
  // An already representable nine-place value is retained; otherwise round upward explicitly.
  const [mantissa, exponent = '0'] = String(margin).toLowerCase().split('e');
  const [whole, fraction = ''] = mantissa.split('.');
  const digits = BigInt(whole + fraction),
    places = 9 + Number(exponent) - fraction.length;
  const power = BigInt('1' + '0'.repeat(Math.abs(places)));
  const units = places >= 0 ? digits * power : (digits + power - BigInt(1)) / power;
  const fractional = (units % BigInt(1_000_000_000)).toString().padStart(9, '0').replace(/0+$/, '');
  return normalizedZoneClearance(`${units / BigInt(1_000_000_000)}${fractional ? `.${fractional}` : ''}`);
}

/** Local mathematical/source binding only; new server save/start must re-resolve currentness. */
export async function validateRemnantPlan(
  value: unknown,
  target: { companyId: number; groupId: string; quote: unknown }
): Promise<RemnantPlan> {
  const plan = planSchema.parse(value);
  unchangedJSON(value, plan);
  requireRemnantDomainProfile(plan.geometryProfile);
  const snapshot = await validateRemnantSnapshot(plan.snapshot);
  const group = remnantTarget(target.quote);
  // Full input validation runs on explicit selection, never on each form keystroke.
  quoteFromFile(target.quote);
  check(
    plan.groupId === target.groupId && snapshot.companyId === target.companyId,
    'The selected piece belongs to another group or company.'
  );
  check(
    (await remnantEvidenceHash(snapshot)) === plan.snapshotSha256,
    'The saved piece snapshot fingerprint does not match.'
  );
  check(
    plan.zoneClearanceIn === normalizedZoneClearance(plan.zoneClearanceIn),
    'Zone clearance must be canonical inches.'
  );
  check(
    plan.assignment.family === group.family &&
      plan.assignment.thicknessIn === group.thicknessIn &&
      snapshot.evidence.thickness === group.thicknessIn &&
      plan.assignment.requiredGrade === snapshot.evidence.grade,
    'The declared family, required grade or thickness does not match this group and reported piece.'
  );
  check(
    (await targetGroupHash({
      groupId: target.groupId,
      requiredGrade: plan.assignment.requiredGrade,
      quote: target.quote,
    })) === plan.assignment.targetGroupSha256,
    'This group has changed. Refresh the piece and reaffirm its material assignment.'
  );
  check(new TextEncoder().encode(JSON.stringify(plan)).length <= 262144, 'The planning selection exceeds 256 KiB.');
  return plan;
}

export async function buildRemnantPlan(input: {
  resolution: RemnantResolution;
  companyId: number;
  groupId: string;
  quote: unknown;
  family: string;
  requiredGrade: string;
  reason: string;
  zoneClearanceIn: string;
}): Promise<RemnantPlan> {
  const snapshot = input.resolution.snapshot;
  await validateRemnantResolution(input.resolution, {
    expected_company_id: input.companyId,
    pieceId: snapshot.pieceId,
    observationNumber: snapshot.observationNumber,
    expected_payload_sha256: snapshot.payloadSha256,
    expected_source_sha256: snapshot.sourceSha256,
  });
  const requiredGrade = input.requiredGrade.trim();
  const plan = {
    version: 1,
    groupId: input.groupId,
    snapshot,
    snapshotSha256: input.resolution.snapshot_sha256,
    assignment: {
      version: 1,
      basis: 'planner_declared_unverified',
      family: input.family,
      requiredGrade,
      thicknessIn: remnantTarget(input.quote).thicknessIn,
      reason: input.reason.trim(),
      targetGroupSha256: await targetGroupHash({ groupId: input.groupId, requiredGrade, quote: input.quote }),
    },
    geometryProfile: { ...REMNANT_DOMAIN_PROFILE },
    zoneClearanceIn: normalizedZoneClearance(input.zoneClearanceIn),
    capacity: 1,
    planningOnly: true,
    eligibilityVerified: false,
    availabilityVerified: false,
  };
  return validateRemnantPlan(plan, input);
}
