/**
 * Customer export control (`/api/v1/customers/{id}/export-control` and
 * `/customers/{id}/contracts`, ADMIN only, never through an API token).
 *
 * Mirrors `backend/app/schemas/export_control.py`. A customer is
 * EXPORT-CONTROLLED when ANY of its contracts (any `active` flag, any dates —
 * the obligations outlive the contract) is ITAR / export controlled / CUI, or
 * classified `itar_export_controlled` / `cui`, or a classified handling
 * instruction says so. An AI-external API token then sees that customer's
 * records redacted. There is no delete: un-marking is an update that clears the
 * flags, with a reason.
 */

/** `app.models.governance.DataClassification`. */
export type DataClassification =
  | 'public'
  | 'internal'
  | 'business_confidential'
  | 'fci'
  | 'cui'
  | 'itar_export_controlled'
  | 'restricted_customer'
  | 'unknown';

export const DATA_CLASSIFICATIONS: ReadonlyArray<{ value: DataClassification; label: string }> = [
  { value: 'public', label: 'Public' },
  { value: 'internal', label: 'Internal' },
  { value: 'business_confidential', label: 'Business confidential' },
  { value: 'fci', label: 'FCI' },
  { value: 'cui', label: 'CUI' },
  { value: 'itar_export_controlled', label: 'ITAR / export controlled' },
  { value: 'restricted_customer', label: 'Restricted customer' },
  { value: 'unknown', label: 'Unknown' },
];

/** Classifications that, on their own, make a contract controlling. */
export const CONTROLLING_CLASSIFICATIONS: ReadonlySet<string> = new Set<string>(['itar_export_controlled', 'cui']);

export function dataClassificationLabel(value: string | null | undefined): string {
  if (!value) return '—';
  return DATA_CLASSIFICATIONS.find(option => option.value === value)?.label ?? value;
}

export interface CustomerContract {
  id: number;
  customer_id: number | null;
  contract_number: string | null;
  contract_name: string;
  itar_controlled: boolean;
  export_controlled: boolean;
  contains_cui: boolean;
  default_data_classification: string;
  effective_date: string | null;
  expiration_date: string | null;
  active: boolean;
  handling_instructions: string | null;
  /** Does THIS contract make its customer export-controlled? */
  controlling: boolean;
  created_by: number | null;
  updated_by: number | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface CustomerExportControl {
  customer_id: number;
  customer_name: string;
  customer_is_deleted: boolean;
  /** Derived server-side — the exact rule AI-external tokens are redacted by. */
  controlled: boolean;
  data_classification: string | null;
  /** Null while controlled means a classified handling instruction controls it. */
  controlling_contract_id: number | null;
  contracts: CustomerContract[];
}

export interface CustomerContractCreateInput {
  contract_name: string;
  contract_number?: string | null;
  itar_controlled: boolean;
  export_controlled: boolean;
  contains_cui: boolean;
  default_data_classification: DataClassification;
  effective_date?: string | null;
  expiration_date?: string | null;
  active: boolean;
  handling_instructions?: string | null;
  /** Required; recorded on the required audit row. */
  reason: string;
}

/** Only the supplied fields change; `reason` is always required. */
export interface CustomerContractUpdateInput {
  contract_name?: string;
  contract_number?: string | null;
  itar_controlled?: boolean;
  export_controlled?: boolean;
  contains_cui?: boolean;
  default_data_classification?: DataClassification;
  effective_date?: string | null;
  expiration_date?: string | null;
  active?: boolean;
  handling_instructions?: string | null;
  reason: string;
}
