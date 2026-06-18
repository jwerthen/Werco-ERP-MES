/**
 * TypeScript contracts for the thermal receiving-label print feature
 * (ProxyBox / WHTP203e bridge).
 *
 * These mirror the backend Pydantic schemas in
 * ``backend/app/schemas/print_profile.py``. SECURITY: the ProxyBox API key is
 * WRITE-ONLY — it is sent on update but the read shape only ever exposes
 * ``api_key_last4`` + ``has_api_key``. The UI must never request, store, or
 * render a full key.
 */

/** Read shape for the per-company print profile. NEVER carries the plaintext key. */
export interface PrintProfile {
  id: number;
  proxybox_base_url?: string | null;
  proxybox_target?: string | null;
  /** Masked tail of the stored key, e.g. "a1b2". Null when none configured. */
  api_key_last4?: string | null;
  has_api_key: boolean;
  default_paper_size?: string | null;
  default_copies?: number | null;
  auto_print_on_receipt: boolean;
  /** Outbound-egress kill switch for the ProxyBox tunnel; defaults OFF. */
  allow_print_egress: boolean;
  is_active: boolean;
  created_at?: string | null;
}

/**
 * Upsert payload. Omitted fields are left unchanged server-side.
 * Sending ``api_key`` rotates the stored ProxyBox key; omit it (or send empty)
 * to keep the existing one. ``api_key`` is write-only and never returned.
 */
export interface PrintProfileUpdate {
  proxybox_base_url?: string | null;
  proxybox_target?: string | null;
  api_key?: string;
  default_paper_size?: string | null;
  default_copies?: number | null;
  auto_print_on_receipt?: boolean;
  allow_print_egress?: boolean;
  is_active?: boolean;
}

/** Outcome of a manual (re)print. */
export interface PrintLabelResponse {
  receipt_id: number;
  receipt_number?: string | null;
  label_document_id?: number | null;
  printed: boolean;
  message?: string | null;
}
