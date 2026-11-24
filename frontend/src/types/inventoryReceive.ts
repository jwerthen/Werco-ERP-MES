import type { OwnershipSelection } from './ownership';

/**
 * `POST /inventory/receive` — Admin / Manager / Supervisor
 * (backend `ReceiveItemRequest` in `app/api/endpoints/inventory.py`).
 *
 * `heat_lot` and `cert_number` are stamped on a NEW stock row only; a receive that
 * merges into an existing row at the same part / location / lot / owner keeps that
 * row's own heat and cert. Optional text fields are omitted rather than sent blank.
 */
export interface ReceiveInventoryRequest extends Partial<OwnershipSelection> {
  part_id: number;
  quantity: number;
  location_code: string;
  lot_number?: string;
  serial_number?: string;
  po_number?: string;
  unit_cost: number;
  cert_number?: string;
  heat_lot?: string;
  notes?: string;
}
