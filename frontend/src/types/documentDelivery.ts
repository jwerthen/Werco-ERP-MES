export type DeliveryEntity = 'quote' | 'purchase_order';
export type DeliveryStatus = 'prepared' | 'sending' | 'accepted' | 'failed' | 'unknown';
export interface DocumentDelivery {
  id: number;
  entity_type: DeliveryEntity;
  entity_id: number;
  document_number: string;
  issue_date?: string | null;
  recipient: string;
  subject: string;
  body: string;
  attachment_name: string;
  attachment_sha256: string;
  attachment_size: number;
  status: DeliveryStatus;
  status_detail: string | null;
  version: number;
  provider_message_id: string | null;
  created_at: string;
  attempted_at: string | null;
  accepted_at: string | null;
  send_available: boolean;
  unavailable_reason: string | null;
  delivered: null;
  replayed: boolean;
  manually_verified: boolean;
  verified_at: string | null;
  verification_note: string | null;
}
export interface DocumentDeliverySend {
  expected_version: number;
  request_key: string;
  recipient: string;
  subject: string;
  body: string;
}

export interface DocumentDeliveryReconcile {
  expected_version: number;
  outcome: 'accepted' | 'failed';
  verification_note: string;
}
