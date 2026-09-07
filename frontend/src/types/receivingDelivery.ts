export interface ReceivingCertificate {
  id: number;
  file_name: string;
  document_number: string;
}
export interface DeliveryLine {
  po_line_id: number;
  quantity_received: number;
  lot_number?: string;
  heat_number?: string;
  serial_numbers?: string;
  cert_number?: string;
  certificate_document_id?: number;
  location_id?: number;
  requires_inspection: boolean;
  over_receive_approved: boolean;
  packing_slip_number?: string;
  carrier?: string;
  tracking_number?: string;
  notes?: string;
}
export interface DeliverySubmission {
  idempotency_key: string;
  purchase_order_id: number;
  lines: DeliveryLine[];
}
export interface DeliveryOutcome {
  batch_id: number;
  idempotency_key: string;
  receipts: {
    id: number;
    receipt_number: string;
    quantity_received: number;
    lot_number: string;
    certificate_document_id?: number | null;
  }[];
}
export interface SupplierConfirmationSubmission {
  expected_updated_at: string | null;
  acknowledged: boolean;
  supplier_confirmed_date: string | null;
  supplier_confirmation_reference: string | null;
  supplier_confirmation_note: string;
  follow_up_owner_id: number | null;
  follow_up_due_date: string | null;
}
