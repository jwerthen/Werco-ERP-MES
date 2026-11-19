import type { OwnershipInfo, OwnershipSelection, OwnershipType } from '../types/ownership';

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
