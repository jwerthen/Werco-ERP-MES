import { z } from 'zod';

export const consumableCheckoutSchema = z.object({
  employee_id: z.string().min(1, 'Select the employee receiving supplies.'),
  inventory_item_id: z.string().min(1, 'Select a consumable and stock location.'),
  quantity: z
    .string()
    .trim()
    .refine(value => Number.isFinite(Number(value)) && Number(value) > 0, 'Enter a quantity greater than zero.'),
  notes: z.string().max(2000, 'Keep notes to 2,000 characters or fewer.'),
});

export type ConsumableCheckoutForm = z.infer<typeof consumableCheckoutSchema>;
