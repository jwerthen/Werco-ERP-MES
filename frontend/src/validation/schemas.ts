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
  .string({
    required_error: 'Part number required',
    invalid_type_error: 'Part number must be a string'
  })
  .min(3, 'Part number must be at least 3 characters')
  .max(50, 'Part number must be at most 50 characters')
  .regex(/^[A-Z0-9-]+$/, 'Only letters, numbers, and dashes allowed')
  .transform((v: string) => v.toUpperCase().trim());

const revisionSchema = z
  .string({
    required_error: 'Revision required',
    invalid_type_error: 'Revision must be a string'
  })
  .min(1, 'Revision required (at least 1 character)')
  .max(20, 'Revision must be at most 20 characters')
  .regex(/^[A-Z0-9]+$/, 'Letters and numbers only')
  .transform((v: string) => v.toUpperCase().trim());

const nameSchema = z
  .string({
    required_error: 'Name required'
  })
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
  .number({
    required_error: 'Amount required',
    invalid_type_error: 'Amount must be a number'
  })
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
  .string({
    required_error: 'Email required'
  })
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
  part_type: z.nativeEnum(PartType, {
    required_error: 'Select a part type',
    invalid_type_error: 'Invalid part type'
  }),
  unit_of_measure: z.nativeEnum(UnitOfMeasure, {
    required_error: 'Select a unit of measure',
    invalid_type_error: 'Invalid unit of measure'
  }),

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

export const partUpdateSchema = z.object({
  version: z.number().int().min(0, 'Version must be 0 or greater'),
  name: nameSchema.optional(),
  revision: revisionSchema.optional(),
  description: descriptionShortSchema,
  unit_of_measure: z.nativeEnum(UnitOfMeasure).optional(),
  standard_cost: moneySchema.optional(),
  material_cost: moneySchema.optional(),
  labor_cost: moneySchema.optional(),
  overhead_cost: moneySchema.optional(),
  lead_time_days: nonNegativeIntegerSchema.max(365).optional(),
  safety_stock: moneySmallSchema.optional(),
  reorder_point: moneySmallSchema.optional(),
  reorder_quantity: moneySmallSchema.optional(),
  is_critical: z.boolean().optional(),
  requires_inspection: z.boolean().optional(),
  inspection_requirements: z.string().max(2000).optional(),
  customer_part_number: z.string().max(100).optional(),
  drawing_number: z.string().max(100).optional(),
  is_active: z.boolean().optional(),
  status: z.string().max(50).optional(),
});

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
  must_ship_by: z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'Invalid date format (YYYY-MM-DD)').optional(),
  customer_name: z.string().max(255).optional(),
  customer_po: z.string().max(50, 'Must be at most 50 characters').optional(),
  lot_number: z.string().max(50, 'Must be at most 50 characters').optional(),
  notes: z.string().max(2000, 'Must be at most 2000 characters').optional(),
  special_instructions: z.string().max(2000, 'Must be at most 2000 characters').optional(),
});

export const workOrderOperationUpdateSchema = z.object({
  version: z.number().int().min(0),
  name: nameSchema.optional(),
  description: descriptionLongSchema,
  setup_instructions: z.string().max(5000).optional(),
  run_instructions: z.string().max(5000).optional(),
  setup_time_hours: z.number().min(0).max(99.99).optional(),
  run_time_hours: z.number().min(0).max(999.99).optional(),
  run_time_per_piece: z.number().min(0).optional(),
  status: z.nativeEnum(OperationStatus).optional(),
  quantity_complete: z.number().min(0).optional(),
  quantity_scrapped: z.number().min(0).optional(),
  requires_inspection: z.boolean().optional(),
  inspection_complete: z.boolean().optional(),
});

export const workOrderUpdateSchema = z.object({
  version: z.number().int().min(0),
  quantity_ordered: z.number().min(0).positive().max(999999.9999).optional(),
  priority: z.number().int().min(1).max(10).optional(),
  status: z.nativeEnum(WorkOrderStatus).optional(),
  due_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional(),
  must_ship_by: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional(),
  customer_name: z.string().max(255).optional(),
  customer_po: z.string().max(50).optional(),
  lot_number: z.string().max(50).optional(),
  notes: z.string().max(2000).optional(),
  special_instructions: z.string().max(2000).optional(),
  quantity_complete: z.number().min(0).optional(),
  quantity_scrapped: z.number().min(0).optional(),
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
    .string({
      required_error: 'Employee ID required'
    })
    .min(1, 'Employee ID required')
    .max(50, 'Must be at most 50 characters')
    .regex(/^[A-Za-z0-9\-_]+$/, 'Letters, numbers, hyphens, and underscores only'),
  first_name: firstNameSchema,
  last_name: lastNameSchema,
  role: z.nativeEnum(UserRole, {
    required_error: 'Select a role',
    invalid_type_error: 'Invalid role'
  }),
  department: z.string().max(100).optional(),
  password: passwordStrengthSchema,
});

export const userUpdateSchema = z.object({
  email: emailSchema.optional(),
  first_name: firstNameSchema.optional(),
  last_name: lastNameSchema.optional(),
  role: z.nativeEnum(UserRole).optional(),
  department: z.string().max(100).optional(),
  is_active: z.boolean().optional(),
});

export const userLoginSchema = z.object({
  email: emailSchema,
  password: z.string().min(1, 'Password required'),
});

export const passwordChangeSchema = z.object({
  current_password: z.string().min(1, 'Current password required'),
  new_password: passwordStrengthSchema,
});

// ============================================================================
// PURCHASING SCHEMA
// ============================================================================

export const vendorSchema = z.object({
  code: z
    .string({
      required_error: 'Vendor code required'
    })
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
  lead_time_days: z.number().int().min(0).max(365).default(14),
  is_approved: z.boolean().default(false),
  is_as9100_certified: z.boolean().default(false),
  is_iso9001_certified: z.boolean().default(false),
  notes: z.string().max(2000).optional(),
});

export const vendorUpdateSchema = z.object({
  version: z.number().int().min(0),
  name: nameSchema.optional(),
  contact_name: z.string().max(100).optional(),
  email: z.string().max(255).optional(),
  phone: z.string().max(50).optional(),
  address_line1: z.string().max(200).optional(),
  address_line2: z.string().max(200).optional(),
  city: z.string().max(100).optional(),
  state: z.string().length(2).regex(/^[A-Z]{2}$/).optional(),
  postal_code: z.string().max(20).optional(),
  country: z.string().length(2).regex(/^[A-Z]{2}$/).optional(),
  payment_terms: z.string().max(100).optional(),
  lead_time_days: z.number().int().min(0).max(365).optional(),
  is_approved: z.boolean().optional(),
  is_as9100_certified: z.boolean().optional(),
  is_iso9001_certified: z.boolean().optional(),
  is_active: z.boolean().optional(),
  notes: z.string().max(2000).optional(),
});

export const poLineSchema = z.object({
  part_id: positiveIntegerSchema,
  quantity_ordered: z.number().min(0).positive('Quantity must be greater than 0').max(999999.9999),
  unit_price: moneySchema,
  required_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional(),
  notes: z.string().max(500).optional(),
});

export const poSchema = z.object({
  vendor_id: positiveIntegerSchema,
  required_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional(),
  expected_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional(),
  ship_to: z.string().max(255).optional(),
  shipping_method: z.string().max(100).optional(),
  notes: z.string().max(2000).optional(),
  lines: z.array(poLineSchema).default([]),
}).refine(
  (data: { required_date?: string; expected_date?: string }) => {
    if (data.required_date && data.expected_date) {
      const required = new Date(data.required_date);
      const expected = new Date(data.expected_date);
      return expected > required;
    }
    return true;
  },
  {
    message: 'Expected date must be after required date',
    path: ['expected_date']
  }
);

export const poUpdateSchema = z.object({
  version: z.number().int().min(0),
  required_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional(),
  expected_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional(),
  ship_to: z.string().max(255).optional(),
  shipping_method: z.string().max(100).optional(),
  notes: z.string().max(2000).optional(),
  status: z.string().optional(),
});

export const receiptCreateSchema = z.object({
  po_line_id: positiveIntegerSchema,
  quantity_received: z.number().min(0).positive('Quantity must be greater than 0').max(999999.9999),
  lot_number: z.string().min(1, 'Lot number required').max(50),
  serial_numbers: z.string().max(500).optional(),
  heat_number: z.string().max(50).optional(),
  cert_number: z.string().max(50).optional(),
  coc_attached: z.boolean().default(false),
  location_id: positiveIntegerSchema.optional(),
  requires_inspection: z.boolean().default(true),
  packing_slip_number: z.string().max(50).optional(),
  carrier: z.string().max(100).optional(),
  tracking_number: z.string().max(100).optional(),
  notes: z.string().max(2000).optional(),
  over_receive_approved: z.boolean().default(false),
});

// ============================================================================
// TYPES
// ============================================================================

export type PartFormData = z.infer<typeof partSchema>;
export type PartUpdateFormData = z.infer<typeof partUpdateSchema>;
export type WorkOrderFormData = z.infer<typeof workOrderSchema>;
export type WorkOrderUpdateFormData = z.infer<typeof workOrderUpdateSchema>;
export type WorkOrderOperationFormData = z.infer<typeof workOrderOperationSchema>;
export type UserFormData = z.infer<typeof userSchema>;
export type UserUpdateFormData = z.infer<typeof userUpdateSchema>;
export type UserLoginFormData = z.infer<typeof userLoginSchema>;
export type VendorFormData = z.infer<typeof vendorSchema>;
export type VendorUpdateFormData = z.infer<typeof vendorUpdateSchema>;
export type POFormData = z.infer<typeof poSchema>;
export type POUpdateFormData = z.infer<typeof poUpdateSchema>;
