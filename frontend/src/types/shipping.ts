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

// ---------------------------------------------------------------------------
// Shipments, the issued Certificate of Conformance, and open quality items.
// Mirrors ``backend/app/api/endpoints/shipping.py`` (list / detail / ready-to-ship
// / ship / coc) and ``quality_gate_service.QualityException.as_dict``.
// ---------------------------------------------------------------------------

export type ReleaseExceptionCode = 'open_ncr' | 'fai_not_passed' | 'open_blocker';
export type ReleaseExceptionReferenceType = 'ncr' | 'fai' | 'work_order_blocker';

/** One open quality item on the job behind a shipment (read only). */
export interface ReleaseException {
  code: ReleaseExceptionCode;
  severity: string;
  message: string;
  reference_type: ReleaseExceptionReferenceType;
  reference_id: number | null;
}

/** A row of ``GET /shipping/`` (``ShipmentResponse``). */
export interface ShipmentSummary {
  id: number;
  shipment_number: string;
  work_order_id: number;
  work_order_number?: string | null;
  customer_name?: string | null;
  part_number?: string | null;
  status: string;
  ship_to_name?: string | null;
  carrier?: string | null;
  tracking_number?: string | null;
  tracking_status?: string | null;
  quantity_shipped: number;
  ship_date?: string | null;
  created_at: string;
  /** The issued, numbered certificate (null until one is issued). */
  coc_number?: string | null;
  coc_issued_at?: string | null;
  /** The automatic certificate was withheld on ship over open quality items. */
  coc_withheld?: boolean;
}

/** ``GET /shipping/{id}``: the shipment with the order facts the packing slip prints. */
export interface ShipmentDetail {
  id: number;
  shipment_number: string;
  work_order_id: number;
  work_order_number: string | null;
  customer_name: string | null;
  customer_po: string | null;
  part_number: string | null;
  part_name: string | null;
  lot_number: string | null;
  status: string;
  ship_to_name: string | null;
  ship_to_address: string | null;
  ship_to_city: string | null;
  ship_to_state: string | null;
  ship_to_zip: string | null;
  po_line_item: string | null;
  unit_number: string | null;
  part_revision: string | null;
  serial_numbers: string[];
  carrier: string | null;
  tracking_number: string | null;
  quantity_shipped: number;
  weight_lbs: number | null;
  num_packages: number | null;
  ship_date: string | null;
  cert_of_conformance: boolean | null;
  packing_notes: string | null;
  created_at: string | null;
  coc_number: string | null;
  coc_issued_at: string | null;
  coc_withheld: boolean;
  release_exceptions: ReleaseException[];
}

/** A row of ``GET /shipping/ready-to-ship``. */
export interface ReadyToShipRow {
  work_order_id: number;
  work_order_number: string;
  part_number?: string | null;
  part_name?: string | null;
  customer_name?: string | null;
  quantity_complete: number;
  quantity_remaining: number;
  quantity_reserved: number;
  quantity_shipped: number;
  due_date?: string | null;
  release_exceptions?: ReleaseException[];
}

/**
 * Accepting the open quality items, sent with Ship or Issue CoC. Only Admin, Manager
 * or Quality may send ``acknowledge_exceptions: true``, and then a reason of at least
 * three characters is required.
 */
export interface ReleaseExceptionAcknowledgment {
  acknowledge_exceptions: boolean;
  reason?: string | null;
}

/** The structured 409 ``detail`` Ship and Issue CoC answer while items are open. */
export interface ReleaseExceptionsConflict {
  code: 'release_exceptions_open';
  message: string;
  release_exceptions: ReleaseException[];
}

/** ``POST /shipping/{id}/ship`` 200 body. */
export interface MarkShippedResult {
  message: string;
  shipment_number: string;
  coc_withheld?: boolean;
  release_exceptions?: ReleaseException[];
}

/** ``CertificateOfConformanceResponse`` (the PDF is a separate route). */
export interface CertificateOfConformance {
  id: number;
  coc_number: string;
  shipment_id: number;
  work_order_id: number;
  customer_name?: string | null;
  customer_po?: string | null;
  part_number?: string | null;
  part_name?: string | null;
  revision?: string | null;
  quantity?: number | null;
  lot_number?: string | null;
  issued_by?: number | null;
  issued_at?: string | null;
}

/** Narrow an unknown Axios error ``detail`` to the open-quality-items 409. */
export function releaseExceptionsConflict(detail: unknown): ReleaseExceptionsConflict | null {
  if (!detail || typeof detail !== 'object') return null;
  const candidate = detail as Partial<ReleaseExceptionsConflict>;
  if (candidate.code !== 'release_exceptions_open' || !Array.isArray(candidate.release_exceptions)) return null;
  return {
    code: 'release_exceptions_open',
    message: typeof candidate.message === 'string' ? candidate.message : '',
    release_exceptions: candidate.release_exceptions,
  };
}
