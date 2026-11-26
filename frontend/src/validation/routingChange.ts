import { z } from 'zod';
import { MAX_SEQUENCE, MIN_SEQUENCE } from '../utils/routingChange';

/** Same width as `work_order_operations.skip_reason` / the server's routing-change reason cap. */
export const ROUTING_REASON_MAX_LENGTH = 500;

const optionalText = (max: number, label: string) =>
  z.string().trim().max(max, `${label} must be at most ${max.toLocaleString()} characters`);

const hours = (label: string) =>
  z.coerce
    .number({ error: `${label}: enter a number` })
    .min(0, `${label} cannot be negative`)
    .max(99999, `${label} is too large`);

/**
 * One routing step in the Add / Edit step dialog (`RoutingOperationModal`). Mirrors
 * `WorkOrderOperationBase` / `WorkOrderOperationUpdate` (backend/app/schemas/work_order.py):
 * sequence 10-990 in tens, name 2-255, instructions up to 5,000, time standards >= 0.
 * Whether `change_reason` is required depends on the work order and on what changed, so the
 * dialog layers that rule on with `withRoutingRules` rather than baking it in here.
 */
export const routingOperationBaseSchema = z.object({
  sequence: z.coerce
    .number({ error: 'Enter a sequence number' })
    .int('Whole numbers only')
    .min(MIN_SEQUENCE, `Sequence must be at least ${MIN_SEQUENCE}`)
    .max(MAX_SEQUENCE, `Sequence must be at most ${MAX_SEQUENCE}`)
    .refine(value => value % 10 === 0, 'Sequence must be a multiple of 10'),
  operation_number: optionalText(20, 'Operation number'),
  name: z.string().trim().min(2, 'Name must be at least 2 characters').max(255, 'Name must be at most 255 characters'),
  work_center_id: z.coerce.number({ error: 'Choose a work center' }).int().positive('Choose a work center'),
  description: optionalText(5000, 'Description'),
  setup_instructions: optionalText(5000, 'Setup instructions'),
  run_instructions: optionalText(5000, 'Run instructions'),
  setup_time_hours: hours('Setup time'),
  run_time_hours: hours('Run time'),
  run_time_per_piece: hours('Run time per piece'),
  requires_inspection: z.boolean(),
  change_reason: optionalText(ROUTING_REASON_MAX_LENGTH, 'Reason'),
});

export type RoutingOperationFormInput = z.input<typeof routingOperationBaseSchema>;
export type RoutingOperationFormData = z.output<typeof routingOperationBaseSchema>;

/** The fields whose change on a released work order the server treats as a reasoned routing change. */
export const ROUTING_CONTENT_FIELDS = [
  'name',
  'description',
  'setup_instructions',
  'run_instructions',
  'setup_time_hours',
  'run_time_hours',
  'run_time_per_piece',
  'requires_inspection',
] as const satisfies readonly (keyof RoutingOperationFormData)[];

export interface RoutingRuleOptions {
  /** Add mode: sequences held by steps that are not skipped (a skipped one's may be reused). */
  usedSequences?: number[];
  /** Is a reason needed for this change at all (the work order is no longer a DRAFT)? */
  reasonRequired: boolean;
  /**
   * Edit mode: the step as it stands. A reason is then needed only when a CONTENT field
   * differs from it — a work-center move alone is reason-free, as on the dispatch board.
   */
  original?: RoutingOperationFormData;
}

export function contentFieldsChanged(values: RoutingOperationFormData, original: RoutingOperationFormData): string[] {
  return ROUTING_CONTENT_FIELDS.filter(field => {
    const next = values[field];
    const prev = original[field];
    if (typeof next === 'number' || typeof prev === 'number') return Number(next) !== Number(prev);
    return (next ?? '') !== (prev ?? '');
  });
}

export function withRoutingRules({ usedSequences, reasonRequired, original }: RoutingRuleOptions) {
  return routingOperationBaseSchema.superRefine((values, ctx) => {
    if (usedSequences && usedSequences.includes(values.sequence)) {
      ctx.addIssue({
        code: 'custom',
        path: ['sequence'],
        message: `Sequence ${values.sequence} is already used on this work order`,
      });
    }
    const needsReason = reasonRequired && (!original || contentFieldsChanged(values, original).length > 0);
    if (needsReason && !values.change_reason) {
      ctx.addIssue({
        code: 'custom',
        path: ['change_reason'],
        message: 'A reason is required to change a released work order',
      });
    }
  });
}
