import { z } from 'zod';

// ============================================================================
// ENUMS
// ============================================================================

export enum PartType {
  MANUFACTURED = 'manufactured',
  PURCHASED = 'purchased',
  ASSEMBLY = 'assembly',
  RAW_MATERIAL = 'raw_material'
}

export enum UnitOfMeasure {
  EACH = 'each',
  FEET = 'feet',
  INCHES = 'inches',
  POUNDS = 'pounds',
  KILOGRAMS = 'kilograms',
  SHEETS = 'sheets',
  GALLONS = 'gallons',
  LITERS = 'liters'
}

export enum UserRole {
  ADMIN = 'admin',
  MANAGER = 'manager',
  SUPERVISOR = 'supervisor',
  OPERATOR = 'operator',
  QUALITY = 'quality',
  SHIPPING = 'shipping',
  VIEWER = 'viewer'
}

export enum WorkOrderStatus {
  NOT_STARTED = 'not_started',
  IN_PROGRESS = 'in_progress',
  COMPLETED = 'completed',
  ON_HOLD = 'on_hold',
  CANCELLED = 'cancelled'
}

export enum OperationStatus {
  NOT_STARTED = 'not_started',
  IN_PROGRESS = 'in_progress',
  COMPLETED = 'completed',
  ON_HOLD = 'on_hold',
  CANCELLED = 'cancelled'
}

// ============================================================================
// REUSABLE SCHEMAS
// ============================================================================

const partNumberSchema = z
  .string({ error: 'Part number required' })
  .min(3, 'Part number must be at least 3 characters')
  .max(50, 'Part number must be at most 50 characters')
  .regex(/^[A-Z0-9-]+$/, 'Only letters, numbers, and dashes allowed')
  .transform((v: string) => v.toUpperCase().trim());

const revisionSchema = z
  .string({ error: 'Revision required' })
  .min(1, 'Revision required (at least 1 character)')
  .max(20, 'Revision must be at most 20 characters')
  .regex(/^[A-Z0-9]+$/, 'Letters and numbers only')
  .transform((v: string) => v.toUpperCase().trim());

const nameSchema = z
  .string({ error: 'Name required' })
  .min(2, 'Name must be at least 2 characters')
  .max(255, 'Name must be at most 255 characters');

const descriptionShortSchema = z
  .string()
  .max(2000, 'Description must be at most 2000 characters')
  .optional();

const descriptionLongSchema = z
  .string()
  .min(20, 'Description must be at least 20 characters')
  .max(5000, 'Description must be at most 5000 characters')
  .optional();

const moneySchema = z
  .number({ error: 'Amount required' })
  .min(0, 'Amount must be positive or zero')
  .max(999999.99, 'Maximum $999,999.99')
  .multipleOf(0.01, 'Maximum 2 decimal places');

const moneySmallSchema = z
  .number()
  .min(0, 'Amount must be positive or zero')
  .max(9999.9999, 'Maximum $9,999.9999')
  .multipleOf(0.0001, 'Maximum 4 decimal places')
  .default(0);

const positiveIntegerSchema = z
  .number()
  .int('Must be an integer')
  .positive('Must be greater than 0');

const nonNegativeIntegerSchema = z
  .number()
  .int('Must be an integer')
  .min(0, 'Must be 0 or greater');

const emailSchema = z
  .string({ error: 'Email required' })
  .email('Enter a valid email address')
  .max(255, 'Email must be at most 255 characters');

const passwordSpecialCharRegex = /[!@#$%^&*()_+=\x5B\x5D{};':"\\|,.<>\x2F?-]/;

// ============================================================================
// PART SCHEMA
// ============================================================================

export const partSchema = z.object({
  part_number: partNumberSchema,
  revision: revisionSchema,
  name: nameSchema,
  description: descriptionShortSchema,
  part_type: z.nativeEnum(PartType, { error: 'Select a part type' }),
  unit_of_measure: z.nativeEnum(UnitOfMeasure, { error: 'Select a unit of measure' }),

  // Costs
  standard_cost: moneySchema.optional().default(0),
  material_cost: moneySchema.optional().default(0),
  labor_cost: moneySchema.optional().default(0),
  overhead_cost: moneySchema.optional().default(0),

  // Lead time
  lead_time_days: nonNegativeIntegerSchema.max(365, 'Lead time must be 0-365 days').default(0),

  // Inventory
  safety_stock: moneySmallSchema.optional().default(0),
  reorder_point: moneySmallSchema.optional().default(0),
  reorder_quantity: moneySmallSchema.optional().default(0),

  // Classification
  is_critical: z.boolean().default(false),
  requires_inspection: z.boolean().default(true),
  inspection_requirements: z.string().max(2000, 'Must be at most 2000 characters').optional(),

  // Customer info
  customer_part_number: z.string().max(100, 'Must be at most 100 characters').optional(),
  drawing_number: z.string().max(100, 'Must be at most 100 characters').optional(),
}).refine(
  (data: { reorder_point?: number; reorder_quantity?: number }) => 
    !((data.reorder_point ?? 0) > 0 && data.reorder_quantity === 0),
  {
    message: 'Reorder quantity must be greater than 0 when reorder point is set',
    path: ['reorder_quantity']
  }
);

// ============================================================================
// WORK ORDER SCHEMA
// ============================================================================

export const workOrderOperationSchema = z.object({
  work_center_id: positiveIntegerSchema,
  sequence: z
    .number()
    .int('Sequence must be an integer')
    .min(10, 'Sequence must be 10-990')
    .max(990, 'Sequence must be 10-990')
    .multipleOf(10, 'Sequence must be a multiple of 10'),
  operation_number: z.string().max(50).optional(),
  name: nameSchema,
  description: descriptionLongSchema,
  setup_instructions: z.string().max(5000, 'Must be at most 5000 characters').optional(),
  run_instructions: z.string().max(5000, 'Must be at most 5000 characters').optional(),
  setup_time_hours: z.number().min(0).max(99.99).default(0),
  run_time_hours: z.number().min(0).max(999.99).default(0),
  run_time_per_piece: z.number().min(0).default(0),
  requires_inspection: z.boolean().default(false),
  inspection_type: z.string().max(100).optional(),
});

export const workOrderSchema = z.object({
  part_id: positiveIntegerSchema,
  quantity_ordered: z.number().min(0).positive('Quantity must be greater than 0').max(999999.9999).default(0),
  priority: z.number().int().min(1, 'Priority must be 1-10').max(10, 'Priority must be 1-10').default(5),
  due_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'Invalid date format (YYYY-MM-DD)').optional(),
  customer_name: z.string().max(255).optional(),
  customer_po: z.string().max(50, 'Must be at most 50 characters').optional(),
  notes: z.string().max(2000, 'Must be at most 2000 characters').optional(),
  special_instructions: z.string().max(2000, 'Must be at most 2000 characters').optional(),
});

// ============================================================================
// USER SCHEMA
// ============================================================================

const firstNameSchema = z.string()
  .min(1, 'First name required')
  .max(50, 'Must be at most 50 characters')
  .regex(/^[a-zA-Z\s-']+$/, 'Letters only (spaces, hyphens, apostrophes allowed)')
  .transform((v: string) => v.trim())
  .transform((v: string) => v.charAt(0).toUpperCase() + v.slice(1));

const lastNameSchema = z.string()
  .min(1, 'Last name required')
  .max(50, 'Must be at most 50 characters')
  .regex(/^[a-zA-Z\s-']+$/, 'Letters only (spaces, hyphens, apostrophes allowed)')
  .transform((v: string) => v.trim())
  .transform((v: string) => v.charAt(0).toUpperCase() + v.slice(1));

const commonPatterns = ['password', '123456', 'qwerty', 'admin', 'letmein', 'welcome'];

const passwordStrengthSchema = z
  .string()
  .min(12, 'Password must be at least 12 characters')
  .max(128, 'Password must be at most 128 characters')
  .regex(/[A-Z]/, 'Password must contain at least one uppercase letter')
  .regex(/[a-z]/, 'Password must contain at least one lowercase letter')
  .regex(/[0-9]/, 'Password must contain at least one number')
  .regex(passwordSpecialCharRegex, 'Password must contain at least one special character')
  .refine(
    (val) => !commonPatterns.some(pattern => val.toLowerCase().includes(pattern)),
    'Password contains a common pattern that is not allowed'
  );

// Password strength calculator for UI feedback
export function calculatePasswordStrength(password: string): {
  score: number;
  label: string;
  color: string;
  requirements: { met: boolean; label: string }[];
} {
  const requirements = [
    { met: password.length >= 12, label: 'At least 12 characters' },
    { met: /[A-Z]/.test(password), label: 'Uppercase letter' },
    { met: /[a-z]/.test(password), label: 'Lowercase letter' },
    { met: /[0-9]/.test(password), label: 'Number' },
    { met: passwordSpecialCharRegex.test(password), label: 'Special character' },
    { met: !commonPatterns.some(p => password.toLowerCase().includes(p)), label: 'No common patterns' },
  ];

  const metCount = requirements.filter(r => r.met).length;
  const score = Math.round((metCount / requirements.length) * 100);

  let label: string;
  let color: string;

  if (score < 40) {
    label = 'Weak';
    color = 'red';
  } else if (score < 70) {
    label = 'Fair';
    color = 'yellow';
  } else if (score < 100) {
    label = 'Good';
    color = 'blue';
  } else {
    label = 'Strong';
    color = 'green';
  }

  return { score, label, color, requirements };
}

export const userSchema = z.object({
  email: emailSchema,
  employee_id: z
    .string({ error: 'Employee ID required' })
    .min(1, 'Employee ID required')
    .max(50, 'Must be at most 50 characters')
    .regex(/^[A-Za-z0-9\-_]+$/, 'Letters, numbers, hyphens, and underscores only'),
  first_name: firstNameSchema,
  last_name: lastNameSchema,
  role: z.nativeEnum(UserRole, { error: 'Select a role' }),
  department: z.string().max(100).optional(),
  password: passwordStrengthSchema,
});

export const userLoginSchema = z.object({
  email: emailSchema,
  password: z.string().min(1, 'Password required'),
});

// ============================================================================
// PURCHASING SCHEMA
// ============================================================================

export const vendorSchema = z.object({
  code: z
    .string({ error: 'Vendor code required' })
    .min(2, 'Code must be at least 2 characters')
    .max(20, 'Code must be at most 20 characters')
    .regex(/^[A-Z0-9-]+$/, 'Letters, numbers, and dashes only')
    .transform((v: string) => v.toUpperCase().trim()),
  name: nameSchema,
  contact_name: z.string().max(100).optional(),
  email: z.string().max(255).optional(),
  phone: z.string().max(50).optional(),
  address_line1: z.string().max(200).optional(),
  address_line2: z.string().max(200).optional(),
  city: z.string().max(100).optional(),
  state: z.string().length(2, 'State must be 2 letters').regex(/^[A-Z]{2}$/).optional(),
  postal_code: z.string().max(20).optional(),
  country: z.string().length(2).regex(/^[A-Z]{2}$/).default('US').optional(),
  payment_terms: z.string().max(100).optional(),
  is_approved: z.boolean().default(false),
  is_as9100_certified: z.boolean().default(false),
  is_iso9001_certified: z.boolean().default(false),
  notes: z.string().max(2000).optional(),
});

// ============================================================================
// LASER NEST (manual entry)
// ============================================================================

// Mirrors backend LaserNestManualCreate / LaserNestUpdate constraints
// (cnc_number 1..100, planned_runs >= 1 int, optional descriptors). Optional
// descriptors are coerced to undefined when blank so a cleared field PATCHes
// as "unset" rather than an empty string.
const optionalTrimmed = (max: number) =>
  z
    .string()
    .max(max)
    .optional()
    .transform((v) => {
      const trimmed = v?.trim();
      return trimmed ? trimmed : undefined;
    });

export const laserNestManualSchema = z.object({
  cnc_number: z
    .string()
    .trim()
    .min(1, 'CNC number is required')
    .max(100, 'CNC number must be at most 100 characters'),
  planned_runs: z.coerce
    .number({ error: 'Enter a number' })
    .int('Whole sheets only')
    .min(1, 'At least 1 run'),
  nest_name: optionalTrimmed(255),
  material: optionalTrimmed(100),
  thickness: optionalTrimmed(50),
  sheet_size: optionalTrimmed(100),
});

// ============================================================================
// PROCESS SHEETS (engineering library)
// ============================================================================

/**
 * StepType values (backend app/models/process_sheet.py StepType). A const
 * array (not an enum) so the parsed form data's `step_type` is exactly the
 * `ProcessSheetStepType` string-literal union in types/processSheet.ts.
 */
export const PROCESS_SHEET_STEP_TYPES = [
  'measurement',
  'checkbox',
  'list',
  'value',
  'photo',
  'file',
  'instruction',
] as const;

/** Sheet header (create + draft-only edit). Mirrors ProcessSheetCreate/Update. */
export const processSheetSchema = z.object({
  title: z
    .string({ error: 'Title required' })
    .trim()
    .min(1, 'Title required')
    .max(255, 'Title must be at most 255 characters'),
  description: z.string().max(5000, 'Description must be at most 5000 characters').optional(),
});

/** Split a textarea into trimmed, non-empty list options (one per line). */
export function parseListOptions(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
}

/**
 * Step-editor form schema. Flat on purpose — the per-type config fields swap
 * in the UI, so the numeric measurement fields are held as STRINGS and parsed
 * in superRefine (an empty <input type="number"> yields '' which
 * z.coerce.number would silently turn into 0 — a real tolerance value).
 *
 * Mirrors backend process_sheet_service._validate_step_definition:
 *   - MEASUREMENT: numeric lsl / nominal / usl with lsl <= nominal <= usl and
 *     lsl < usl; unit required client-side; decimals optional whole number.
 *   - LIST: at least one non-empty option.
 *   - INSTRUCTION: never required (the modal forces is_required=false and
 *     disables the toggle).
 *   - requires_gauge / spc_characteristic_id: MEASUREMENT-only (the modal
 *     zeroes them for other types at payload build).
 */
export const processSheetStepSchema = z
  .object({
    sequence: z.coerce
      .number({ error: 'Enter a sequence number' })
      .int('Whole numbers only')
      .positive('Must be greater than 0'),
    label: z
      .string({ error: 'Label required' })
      .trim()
      .min(1, 'Label required')
      .max(255, 'Label must be at most 255 characters'),
    instruction_text: z.string().max(5000, 'Must be at most 5000 characters').optional(),
    step_type: z.enum(PROCESS_SHEET_STEP_TYPES, { error: 'Select a step type' }),
    is_required: z.boolean(),
    requires_gauge: z.boolean(),
    // 0 = "none" sentinel from the select; mapped to null at payload build.
    spc_characteristic_id: z.coerce.number().int().min(0),
    // MEASUREMENT config (strings — see docblock).
    nominal: z.string().optional(),
    lsl: z.string().optional(),
    usl: z.string().optional(),
    unit: z.string().max(50, 'Unit must be at most 50 characters').optional(),
    decimals: z.string().optional(),
    // LIST config: one option per line.
    options_text: z.string().optional(),
    // PHOTO / FILE config.
    hint: z.string().max(255, 'Hint must be at most 255 characters').optional(),
  })
  .superRefine((data, ctx) => {
    if (data.step_type === 'measurement') {
      const parseNumber = (raw: string | undefined, field: string, label: string): number | null => {
        const trimmed = (raw ?? '').trim();
        if (!trimmed) {
          ctx.addIssue({ code: 'custom', path: [field], message: `${label} required` });
          return null;
        }
        const num = Number(trimmed);
        if (!Number.isFinite(num)) {
          ctx.addIssue({ code: 'custom', path: [field], message: `${label} must be a number` });
          return null;
        }
        return num;
      };

      const lsl = parseNumber(data.lsl, 'lsl', 'LSL');
      const nominal = parseNumber(data.nominal, 'nominal', 'Nominal');
      const usl = parseNumber(data.usl, 'usl', 'USL');

      if (!(data.unit ?? '').trim()) {
        ctx.addIssue({ code: 'custom', path: ['unit'], message: 'Unit required' });
      }

      if (lsl !== null && nominal !== null && usl !== null) {
        if (!(lsl <= nominal && nominal <= usl)) {
          ctx.addIssue({
            code: 'custom',
            path: ['nominal'],
            message: 'Limits must satisfy LSL ≤ nominal ≤ USL',
          });
        }
        if (!(lsl < usl)) {
          ctx.addIssue({ code: 'custom', path: ['usl'], message: 'LSL must be less than USL' });
        }
      }

      const decimalsRaw = (data.decimals ?? '').trim();
      if (decimalsRaw) {
        const decimals = Number(decimalsRaw);
        if (!Number.isInteger(decimals) || decimals < 0 || decimals > 6) {
          ctx.addIssue({
            code: 'custom',
            path: ['decimals'],
            message: 'Decimals must be a whole number between 0 and 6',
          });
        }
      }
    }

    if (data.step_type === 'list') {
      if (parseListOptions(data.options_text ?? '').length === 0) {
        ctx.addIssue({
          code: 'custom',
          path: ['options_text'],
          message: 'At least one option required (one per line)',
        });
      }
    }
  });

// ============================================================================
// TYPES
// ============================================================================

// Output (post-coercion: planned_runs is a number, optional fields are string|undefined).
export type LaserNestManualFormData = z.output<typeof laserNestManualSchema>;
// Input (what the form fields hold before coercion: planned_runs may be a string).
export type LaserNestManualFormInput = z.input<typeof laserNestManualSchema>;
export type PartFormData = z.infer<typeof partSchema>;
export type ProcessSheetFormData = z.output<typeof processSheetSchema>;
// Output (post-coercion: sequence is a number) vs input (form fields pre-coercion).
export type ProcessSheetStepFormData = z.output<typeof processSheetStepSchema>;
export type ProcessSheetStepFormInput = z.input<typeof processSheetStepSchema>;
export type WorkOrderFormData = z.infer<typeof workOrderSchema>;
export type WorkOrderOperationFormData = z.infer<typeof workOrderOperationSchema>;
export type UserFormData = z.infer<typeof userSchema>;
export type UserLoginFormData = z.infer<typeof userLoginSchema>;
export type VendorFormData = z.infer<typeof vendorSchema>;
