/**
 * Customer- and supplier-owned property (AS9100D 8.5.3).
 *
 * A stock row (`inventory_items`) or a tool (`tools`) is owned by the company, by one
 * customer, or by one supplier. The server enforces the pairing with a CHECK: `company`
 * carries no owner id, `customer` carries exactly `owner_customer_id`, `supplier`
 * carries exactly `owner_vendor_id`. A row the server has not yet migrated (or an old
 * cached payload) carries none of the three fields — read that as `company`, which is
 * exactly what every pre-existing row became.
 */
export type OwnershipType = 'company' | 'customer' | 'supplier';

/** The three fields every ownership write sends, always together. */
export interface OwnershipSelection {
  ownership: OwnershipType;
  owner_customer_id: number | null;
  owner_vendor_id: number | null;
}

/** What a read carries: the three fields plus the tenant-resolved owner name. */
export interface OwnershipInfo {
  ownership?: OwnershipType | null;
  owner_customer_id?: number | null;
  owner_vendor_id?: number | null;
  owner_name?: string | null;
}

/**
 * `POST /inventory/items/{item_id}/ownership` — the reasoned change-of-ownership verb.
 * Admin / Manager / Supervisor. `reason` is required and non-blank; the server refuses
 * (409) an inactive or zero-quantity row and a no-op, and writes a required audit row
 * (503 when it cannot). No ledger row: ownership is not a quantity movement.
 */
export interface InventoryOwnershipChange extends OwnershipSelection {
  reason: string;
}

/** An open work-order tie PINNED to the re-owned row: a pin consumes its lot whoever owns it. */
export interface OwnershipPinnedTie {
  allocation_id: number;
  work_order_id: number;
  work_order_number: string | null;
  /** Planned less consumed — what this tie still expects to draw at completion. */
  outstanding_quantity: number;
}

/**
 * Only when COMPANY stock is re-owned away: the part's open UNPINNED demand against the
 * company stock left to cover it. Automatic (FIFO) consumption no longer reads the
 * re-owned row, so `short_by` is what those jobs can no longer draw.
 */
export interface OwnershipUnpinnedDemand {
  open_unpinned_demand: number;
  company_stock_remaining: number;
  short_by: number;
  work_order_numbers: string[];
}

/**
 * What `POST /inventory/items/{item_id}/ownership` returns: the updated `inventory_items`
 * row (timestamps UTC ISO-8601 with a trailing `Z`), its resolved `owner_name`, and two
 * DISCLOSURES that never refuse the change and are also written to the audit row.
 */
export interface InventoryOwnershipChangeResult extends OwnershipInfo {
  id: number;
  part_id: number;
  location: string;
  warehouse: string | null;
  quantity_on_hand: number;
  quantity_allocated: number | null;
  quantity_available: number | null;
  lot_number: string | null;
  serial_number: string | null;
  status: string | null;
  is_active: boolean | null;
  received_date: string | null;
  expiration_date: string | null;
  created_at: string | null;
  updated_at: string | null;
  owner_name: string | null;
  open_pinned_ties: OwnershipPinnedTie[];
  unpinned_demand: OwnershipUnpinnedDemand | null;
}

/** A row in the lazily-loaded owner pickers. */
export interface OwnerOption {
  id: number;
  name: string;
  /** Vendors only: an inactive vendor stays a valid owner (only a deleted one is not). */
  is_active?: boolean;
  code?: string | null;
}
