import type { HankReceiveInput, HankProductionInput, HankShipmentInput } from './hankWork';

export type HankBasicActionKind = 'repeat_job' | 'draft_purchase_order' | 'attach_document';
export type HankOperationalActionKind = 'receive_delivery' | 'report_production' | 'draft_shipment';
export type HankActionKind = HankBasicActionKind | HankOperationalActionKind;
export type HankTaskKind = HankActionKind | 'watch_work_order';

export interface HankCapabilities {
  company_id: number;
  allowed_kinds: HankActionKind[];
  can_write: boolean;
  can_watch: boolean;
}

export interface HankTaskReference {
  type: string;
  id: number;
  label: string;
  url: string;
}

export interface HankTask {
  id: number;
  company_id: number;
  kind: HankTaskKind;
  title: string;
  status: 'awaiting_review' | 'completed' | 'cancelled' | 'needs_attention' | 'watching' | 'snoozed';
  version: number;
  input: Record<string, unknown>;
  preview: {
    summary: string;
    changes: string[];
    warnings: string[];
    references: HankTaskReference[];
  };
  result: {
    summary: string;
    warnings: string[];
    references: HankTaskReference[];
  } | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  last_checked_at?: string | null;
  snoozed_until?: string | null;
}

interface HankTaskCreateBase {
  expected_company_id: number;
  request_key: string;
}

export type HankTaskCreate = HankTaskCreateBase &
  (
    | {
        kind: 'repeat_job';
        input: { source_work_order_id: number; quantity_ordered: number; due_date?: string | null };
      }
    | {
        kind: 'draft_purchase_order';
        input: {
          vendor_id: number;
          source_intake_file_id?: number;
          source_intake_version?: number;
          po_number?: string;
          order_date?: string | null;
          ready_for_receiving?: boolean;
          required_date?: string | null;
          expected_date?: string | null;
          ship_to?: string;
          shipping_method?: string;
          notes?: string;
          lines: Array<{
            part_id: number;
            source_line_index?: number;
            unit_of_measure?: string;
            quantity_ordered: number;
            unit_price: number;
            required_date?: string | null;
            notes?: string;
          }>;
        };
      }
    | { kind: 'attach_document'; input: { document_id: number; work_order_id: number } }
    | { kind: 'receive_delivery'; input: HankReceiveInput }
    | { kind: 'report_production'; input: HankProductionInput }
    | { kind: 'draft_shipment'; input: HankShipmentInput }
  );

export interface HankTaskCommand {
  expected_company_id: number;
  expected_version: number;
}

export interface HankTaskList {
  tasks: HankTask[];
  has_more: boolean;
  next_before_id: number | null;
}
