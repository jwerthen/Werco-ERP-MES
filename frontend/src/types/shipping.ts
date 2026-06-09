/**
 * TypeScript contracts for the multi-carrier shipping integration.
 *
 * These mirror the backend Pydantic schemas in
 * ``backend/app/schemas/shipping.py``. SECURITY: a carrier account's API key /
 * webhook secret are WRITE-ONLY -- they are sent on create/update but the read
 * shape only ever exposes ``api_key_last4`` + ``has_webhook_secret``. The UI
 * must never request, store, or render a full key.
 */

export type CarrierProvider = 'easypost' | 'zenkraft';
export type CarrierEnvironment = 'production' | 'test';
export type ShipMode = 'parcel' | 'freight';

// ---------------------------------------------------------------------------
// Carrier account (credentials) admin CRUD.
// ---------------------------------------------------------------------------

/** Read shape for a carrier account. NEVER carries the plaintext key/secret. */
export interface CarrierAccount {
  id: number;
  name: string;
  provider: string;
  environment?: string | null;
  is_active?: boolean | null;
  is_default?: boolean | null;
  /** Carrier-ref KEYS only (no values), e.g. ["fedex", "ups"]. */
  carrier_refs: string[];
  /** Masked tail of the stored key, e.g. "a1b2". Null if undecryptable. */
  api_key_last4?: string | null;
  has_webhook_secret: boolean;
  created_at?: string | null;
}

/** Create payload. ``api_key`` / ``webhook_secret`` are write-only secrets. */
export interface CarrierAccountCreate {
  name: string;
  provider: string;
  environment?: string;
  api_key: string;
  webhook_secret?: string | null;
  carrier_refs?: Record<string, string>;
  is_active?: boolean;
  is_default?: boolean;
}

/** Patch payload. Sending ``api_key`` / ``webhook_secret`` rotates the secret. */
export interface CarrierAccountUpdate {
  name?: string;
  environment?: string;
  api_key?: string;
  webhook_secret?: string | null;
  carrier_refs?: Record<string, string>;
  is_active?: boolean;
  is_default?: boolean;
}

export interface CarrierConnectionTestResult {
  ok: boolean;
  provider: string;
  message?: string | null;
}

// ---------------------------------------------------------------------------
// Company shipping profile (ship-from origin + egress kill switch).
// ---------------------------------------------------------------------------

export interface CompanyShippingProfileBase {
  ship_from_name?: string | null;
  ship_from_company?: string | null;
  ship_from_phone?: string | null;
  ship_from_email?: string | null;
  ship_from_street1?: string | null;
  ship_from_street2?: string | null;
  ship_from_city?: string | null;
  ship_from_state?: string | null;
  ship_from_zip?: string | null;
  ship_from_country?: string | null;
  default_package_weight_lbs?: number | string | null;
  default_package_length_in?: number | string | null;
  default_package_width_in?: number | string | null;
  default_package_height_in?: number | string | null;
}

export interface CompanyShippingProfile extends CompanyShippingProfileBase {
  id: number;
  /** Customer-data egress kill switch. Defaults OFF until CUI/DoD sign-off. */
  allow_carrier_egress: boolean;
  created_at?: string | null;
}

export interface CompanyShippingProfileUpdate extends CompanyShippingProfileBase {
  /** Omit to leave unchanged; the egress kill switch is audited when flipped. */
  allow_carrier_egress?: boolean;
}

// ---------------------------------------------------------------------------
// Address validation.
// ---------------------------------------------------------------------------

/** A label-grade postal address at the API boundary. */
export interface ShippingAddress {
  name?: string | null;
  company?: string | null;
  phone?: string | null;
  email?: string | null;
  street1: string;
  street2?: string | null;
  city: string;
  state: string;
  zip: string;
  country?: string;
  residential?: boolean | null;
}

export interface AddressValidationRequest {
  address: ShippingAddress;
}

export interface AddressValidationResult {
  is_valid: boolean;
  normalized: ShippingAddress;
  messages: string[];
  deliverability?: string | null;
}

// ---------------------------------------------------------------------------
// Packages (parcels + pallets) and rate-shop.
// ---------------------------------------------------------------------------

export interface ParcelInput {
  length_in: number | string;
  width_in: number | string;
  height_in: number | string;
  weight_lbs: number | string;
}

export interface PalletInput {
  length_in: number | string;
  width_in: number | string;
  height_in: number | string;
  weight_lbs: number | string;
  freight_class?: string | null;
  nmfc?: string | null;
  stackable?: boolean;
}

export interface RateShopRequest {
  carrier_account_id?: number | null;
  ship_from?: ShippingAddress | null;
  ship_to?: ShippingAddress | null;
  parcels?: ParcelInput[];
  pallets?: PalletInput[];
}

export interface RateQuote {
  /** Persisted ShipmentRateQuote id (null for transient). */
  id?: number | null;
  provider_rate_id: string;
  carrier: string;
  service_code?: string | null;
  service_name?: string | null;
  mode: ShipMode;
  amount: number | string;
  currency: string;
  est_delivery_days?: number | null;
  est_delivery_date?: string | null;
  is_selected: boolean;
}

// ---------------------------------------------------------------------------
// Label / BOL purchase, pickups, void/refund.
// ---------------------------------------------------------------------------

export interface BuyLabelRequest {
  rate_id: string;
  carrier_account_id?: number | null;
}

export interface BuyBolRequest {
  rate_id: string;
  carrier_account_id?: number | null;
}

export interface BuyLabelResult {
  shipment_id: number;
  shipment_number: string;
  carrier?: string | null;
  service_code?: string | null;
  tracking_number?: string | null;
  actual_cost?: number | string | null;
  cost_currency?: string | null;
  label_document_id?: number | null;
  label_purchased_at?: string | null;
  already_purchased: boolean;
}

export interface BuyBolResult {
  shipment_id: number;
  shipment_number: string;
  carrier?: string | null;
  bol_number?: string | null;
  pro_number?: string | null;
  actual_cost?: number | string | null;
  cost_currency?: string | null;
  bol_document_id?: number | null;
  label_purchased_at?: string | null;
  already_purchased: boolean;
}

export interface SchedulePickupRequest {
  /** ISO date, e.g. 2026-06-10. */
  pickup_date: string;
  /** ISO datetime for the earliest pickup window. */
  window_start: string;
  /** ISO datetime for the latest pickup window. */
  window_end: string;
  carrier_account_id?: number | null;
}

export interface SchedulePickupResult {
  provider_pickup_id: string;
  confirmation_number?: string | null;
  scheduled_date?: string | null;
  window_start?: string | null;
  window_end?: string | null;
  status?: string | null;
}

export interface VoidRefundResult {
  shipment_id: number;
  voided_at?: string | null;
  refund_status?: string | null;
  message?: string | null;
}

// ---------------------------------------------------------------------------
// Tracking.
// ---------------------------------------------------------------------------

export interface TrackingEvent {
  id?: number | null;
  status?: string | null;
  status_detail?: string | null;
  occurred_at?: string | null;
  location?: string | null;
  message?: string | null;
  source?: string | null;
  provider_event_id?: string | null;
  created_at?: string | null;
}

export interface ShipmentTracking {
  shipment_id: number;
  shipment_number: string;
  tracking_number?: string | null;
  tracking_status?: string | null;
  tracking_status_detail?: string | null;
  last_tracking_sync_at?: string | null;
  actual_delivery?: string | null;
  events: TrackingEvent[];
}
