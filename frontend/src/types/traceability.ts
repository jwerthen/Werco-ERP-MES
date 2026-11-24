/**
 * Lot / serial traceability contracts — `GET /traceability/lot/{lot}`,
 * `GET /traceability/serial/{serial}` and `GET /traceability/search`
 * (backend `app/services/traceability_service.py`).
 *
 * Heat, cert and the certificate document fall back to the lot's PO receipt
 * (matched on lot AND part) whenever the stock row is blank, so a PO-received
 * lot shows them even though receiving stored them on the receipt.
 */

export interface TraceHistoryItem {
  timestamp: string;
  event_type: string;
  /** Always present on a lot trace; a serial trace leaves it out. */
  description?: string;
  quantity?: number | null;
  location?: string | null;
  reference?: string | null;
  reference_type?: string | null;
  user?: string | null;
  /**
   * Free-form per event. A `received` event carries `heat_number`, `cert_number`,
   * `certificate_document_id`, `packing_slip` and `inspection_status`.
   */
  details?: Record<string, unknown> | null;
}

export interface TraceConsumedComponent {
  work_order_id: number;
  work_order_number?: string | null;
  component_part_id?: number | null;
  component_part_number?: string | null;
  component_part_name?: string | null;
  lot_number?: string | null;
  quantity: number;
}

export interface LotTraceResponse {
  lot_number: string;
  part_id?: number | null;
  part_number?: string | null;
  part_name?: string | null;
  supplier_name?: string | null;
  po_number?: string | null;
  received_date?: string | null;
  cert_number?: string | null;
  heat_lot?: string | null;
  /** The mill cert / CoC file attached at receiving (a `documents` row id), or null. */
  certificate_document_id?: number | null;
  current_quantity: number;
  current_location?: string | null;
  status: string;
  work_orders_used: string[];
  shipments: string[];
  ncrs: string[];
  consumed_components: TraceConsumedComponent[];
  history: TraceHistoryItem[];
}

export interface SerialTraceResponse {
  serial_number: string;
  part_number?: string | null;
  part_name?: string | null;
  lot_number?: string | null;
  current_location?: string | null;
  status: string;
  received_date?: string | null;
  cert_number?: string | null;
  work_orders_used: string[];
  ncrs: string[];
  history: TraceHistoryItem[];
}

export interface TraceSearchResult {
  type: 'lot' | 'serial';
  number: string;
  part_number?: string | null;
  part_name?: string | null;
  /** On hand for that lot and part; 0 for a lot still held for inspection. */
  quantity?: number | null;
  location?: string | null;
  /** Lot hits only. */
  heat_lot?: string | null;
  cert_number?: string | null;
}
