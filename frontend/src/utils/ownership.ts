import type {
  InventoryOwnershipChangeResult,
  OwnershipInfo,
  OwnershipSelection,
  OwnershipType,
} from '../types/ownership';

export const COMPANY_OWNERSHIP: OwnershipSelection = {
  ownership: 'company',
  owner_customer_id: null,
  owner_vendor_id: null,
};

export const OWNERSHIP_LABELS: Record<OwnershipType, string> = {
  company: 'Company-owned',
  customer: 'Customer-owned',
  supplier: 'Supplier-owned',
};

/** A missing / unknown value is company-owned: every pre-existing row became `company`. */
export function ownershipOf(record: OwnershipInfo | null | undefined): OwnershipType {
  const value = record?.ownership;
  return value === 'customer' || value === 'supplier' ? value : 'company';
}

/**
 * "Customer: {owner_name}" / "Supplier: {owner_name}", or `null` for company stock —
 * company ownership is the default and is deliberately left unlabelled so the chip
 * only appears on the rows that are not the company's.
 */
export function ownershipLabel(record: OwnershipInfo | null | undefined): string | null {
  const kind = ownershipOf(record);
  if (kind === 'company') return null;
  const name = record?.owner_name?.trim();
  const fallbackId = kind === 'customer' ? record?.owner_customer_id : record?.owner_vendor_id;
  const owner = name || (fallbackId ? `#${fallbackId}` : 'unknown owner');
  return `${kind === 'customer' ? 'Customer' : 'Supplier'}: ${owner}`;
}

/** A selection is sendable when its owner id matches its kind (the server's CHECK). */
export function isOwnershipComplete(selection: OwnershipSelection): boolean {
  if (selection.ownership === 'customer') return !!selection.owner_customer_id;
  if (selection.ownership === 'supplier') return !!selection.owner_vendor_id;
  return true;
}

/**
 * The three fields exactly as the server's CHECK wants them: the id that does not
 * belong to the chosen kind is always sent as null, so switching kind back and forth
 * in a form can never post a stale owner alongside the new one.
 */
export function ownershipPayload(selection: OwnershipSelection): OwnershipSelection {
  return {
    ownership: selection.ownership,
    owner_customer_id: selection.ownership === 'customer' ? selection.owner_customer_id : null,
    owner_vendor_id: selection.ownership === 'supplier' ? selection.owner_vendor_id : null,
  };
}

/** Same owner — used to keep a no-op change-of-ownership from reaching the server. */
export function sameOwnership(a: OwnershipSelection, b: OwnershipInfo | null | undefined): boolean {
  const current = ownershipPayload({
    ownership: ownershipOf(b),
    owner_customer_id: b?.owner_customer_id ?? null,
    owner_vendor_id: b?.owner_vendor_id ?? null,
  });
  const next = ownershipPayload(a);
  return (
    next.ownership === current.ownership &&
    next.owner_customer_id === current.owner_customer_id &&
    next.owner_vendor_id === current.owner_vendor_id
  );
}

/** At most this many work orders are named in a toast; the rest are counted. */
const MAX_LISTED_WORK_ORDERS = 5;

function listWorkOrders(numbers: string[]): string {
  const unique = Array.from(new Set(numbers.filter(Boolean)));
  if (unique.length <= MAX_LISTED_WORK_ORDERS) return unique.join(', ');
  return `${unique.slice(0, MAX_LISTED_WORK_ORDERS).join(', ')} and ${unique.length - MAX_LISTED_WORK_ORDERS} more`;
}

const formatQuantity = (quantity: number) => quantity.toLocaleString(undefined, { maximumFractionDigits: 4 });

export interface OwnershipChangeNotice {
  type: 'success' | 'warning';
  message: string;
}

/**
 * The toast for a SUCCESSFUL change of ownership. The change always went through; the
 * question is whether open jobs now need someone's attention:
 *
 *  - `open_pinned_ties`, when the row is now customer- or supplier-owned: those jobs are
 *    pinned to this exact row and a pin consumes its lot whoever owns it, so they will
 *    use someone else's property at completion. Pins on a row returned to the company
 *    draw company stock as normal and need no warning.
 *  - `unpinned_demand.short_by > 0`: company stock was re-owned away and the stock left
 *    no longer covers the open unpinned demand, so those jobs will come up short.
 *    Demand the remaining company stock still covers needs no action.
 *
 * Either one makes it a `warning` (succeeded, but act on it) naming the work orders;
 * otherwise a plain success.
 */
export function ownershipChangeNotice(result: InventoryOwnershipChangeResult): OwnershipChangeNotice {
  const parts: string[] = [];
  const pinned = ownershipOf(result) === 'company' ? [] : (result.open_pinned_ties ?? []);
  if (pinned.length) {
    const jobs = Array.from(new Set(pinned.map(tie => tie.work_order_number || `work order #${tie.work_order_id}`)));
    const target = result.lot_number ? 'this lot' : 'this stock';
    parts.push(
      `${jobs.length} open job${jobs.length === 1 ? ' is' : 's are'} pinned to ${target} (${listWorkOrders(jobs)}) ` +
        'and will still consume it at completion'
    );
  }
  const demand = result.unpinned_demand;
  if (demand && demand.short_by > 0) {
    const jobs = listWorkOrders(demand.work_order_numbers ?? []);
    parts.push(`company stock is now short by ${formatQuantity(demand.short_by)} for ${jobs || 'open jobs'}`);
  }
  if (!parts.length) return { type: 'success', message: 'Ownership changed.' };
  const detail = parts.join('; ');
  return { type: 'warning', message: `Ownership changed. ${detail.charAt(0).toUpperCase()}${detail.slice(1)}.` };
}
