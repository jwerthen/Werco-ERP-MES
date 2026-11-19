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

/** A row in the lazily-loaded owner pickers. */
export interface OwnerOption {
  id: number;
  name: string;
  /** Vendors only: an inactive vendor stays a valid owner (only a deleted one is not). */
  is_active?: boolean;
  code?: string | null;
}
