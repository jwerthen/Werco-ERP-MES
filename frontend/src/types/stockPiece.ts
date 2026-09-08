/** Reported physical evidence only; these records are never supply or reservations. */
export const STOCK_PIECE_ADVISORY = 'Recorded observation — availability and eligibility unverified.';
export type InchPoint = { x: string; y: string };
export type ObservedCircle = { kind: 'circle'; cx: string; cy: string; r: string };
export type ObservedLoop = ObservedCircle | { kind: 'polygon'; pts: InchPoint[] };
export type ObservedShape =
  | { kind: 'unknown' }
  | { kind: 'rectangle'; width: string; height: string }
  | ObservedCircle
  | { kind: 'polygon'; outer: InchPoint[]; holes: InchPoint[][] };
export type ObservedZone = { id: string; label: string; reason: string; outline: ObservedLoop };
export type StockPieceEvidence = {
  version: 1;
  unit: 'in';
  measurement_method: string;
  source_units: 'in' | 'mm' | 'unknown';
  geometry: ObservedShape;
  unavailable_zones: ObservedZone[];
  thickness: string | null;
  grade: string | null;
  grain_axis: 'x' | 'y' | null;
  location_note: string | null;
  ownership_note: string | null;
  certification_note: string | null;
};
export type StockPieceSource = {
  inventory_item_id: number;
  part_id: number;
  source_sha256: string;
  snapshot: Record<string, unknown>;
  review_issues: string[];
  advisory: typeof STOCK_PIECE_ADVISORY;
};
export type StockPieceSummary = {
  piece_id: number;
  company_id: number;
  label: string;
  observation_number: number;
  piece_version: number;
  state: 'RECORDED' | 'WITHDRAWN';
  reason: string;
  observed_at: string;
  observer_name: string;
  created_at: string;
  created_by: number;
  submitted_api_token_id: number | null;
  payload_schema_version: 1;
  payload_sha256: string;
  payload_bytes: number;
  source_inventory_item_id: number;
  source_part_id: number;
  source_sha256: string;
  source_status: 'unchanged' | 'changed' | 'missing';
  current_source_sha256: string | null;
  review_issues: string[];
  advisory: typeof STOCK_PIECE_ADVISORY;
};
export type StockPieceDetail = StockPieceSummary & {
  evidence: StockPieceEvidence;
  source_snapshot: Record<string, unknown>;
  request_key: string;
};
export type StockPiecePage<T> = {
  company_id: number;
  can_record: boolean;
  items: T[];
  total: number;
  page: number;
  per_page: number;
};
export type ObservationCommand = {
  expected_company_id: number;
  request_key: string;
  reason: string;
  observed_at: string;
  observer_name: string;
};
export type RecordedObservation = ObservationCommand & {
  state: 'RECORDED';
  source_inventory_item_id: number;
  source_part_id: number;
  expected_source_sha256: string;
  evidence: StockPieceEvidence;
};
export type CreateStockPiece = RecordedObservation & { label: string };
export type AppendStockPieceObservation =
  | (RecordedObservation & { expected_version: number })
  | (ObservationCommand & { state: 'WITHDRAWN'; expected_version: number });
