import type {
  AppendStockPieceObservation,
  CreateStockPiece,
  StockPieceDetail,
  StockPiecePage,
  StockPieceSource,
  StockPieceSummary,
} from '../../types/stockPiece';
import { STOCK_PIECE_ADVISORY } from '../../types/stockPiece';
import { stockPieceEvidenceSchema } from '../../validation/stockPiece';

export function observationError(error: unknown): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail))
    return detail.map(item => (typeof item?.msg === 'string' ? item.msg : 'Invalid field')).join(' ');
  return error instanceof Error
    ? error.message
    : 'The request did not finish. Retry the same request to recover its result.';
}
export function checkObservationPage<T>(result: StockPiecePage<T>, companyId: number) {
  if (
    result.company_id !== companyId ||
    typeof result.can_record !== 'boolean' ||
    !Array.isArray(result.items) ||
    !Number.isSafeInteger(result.total) ||
    !Number.isSafeInteger(result.page) ||
    result.page < 1 ||
    !Number.isSafeInteger(result.per_page) ||
    result.per_page < 1
  )
    throw new Error('The observation response is invalid or belongs to another company. Refresh the page.');
}
export function checkObservation(result: StockPieceSummary, companyId: number) {
  if (
    result.company_id !== companyId ||
    result.advisory !== STOCK_PIECE_ADVISORY ||
    !Number.isSafeInteger(result.piece_id) ||
    result.piece_id < 1 ||
    !Number.isSafeInteger(result.observation_number) ||
    result.observation_number < 1 ||
    result.piece_version !== result.observation_number ||
    !/^[0-9a-f]{64}$/.test(result.payload_sha256) ||
    !['RECORDED', 'WITHDRAWN'].includes(result.state)
  )
    throw new Error('The observation receipt is invalid or belongs to another company.');
}
export function checkObservationDetail(result: StockPieceDetail, companyId: number) {
  checkObservation(result, companyId);
  stockPieceEvidenceSchema.parse(result.evidence);
  checkSource(
    {
      inventory_item_id: result.source_inventory_item_id,
      part_id: result.source_part_id,
      source_sha256: result.source_sha256,
      snapshot: result.source_snapshot,
      review_issues: result.review_issues,
      advisory: result.advisory,
    },
    companyId
  );
}
export function checkSource(source: StockPieceSource, companyId: number) {
  const item = snapshotObject(source.snapshot, 'item'),
    part = snapshotObject(source.snapshot, 'part');
  if (
    source.advisory !== STOCK_PIECE_ADVISORY ||
    !Number.isSafeInteger(source.inventory_item_id) ||
    source.inventory_item_id < 1 ||
    !Number.isSafeInteger(source.part_id) ||
    source.part_id < 1 ||
    item.company_id !== companyId ||
    part.company_id !== companyId ||
    item.id !== source.inventory_item_id ||
    item.part_id !== source.part_id ||
    part.id !== source.part_id ||
    !/^[0-9a-f]{64}$/.test(source.source_sha256)
  )
    throw new Error('Source response identity is invalid or belongs to another company.');
}
function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record)
      .sort()
      .map(key => `${JSON.stringify(key)}:${canonicalJson(record[key])}`)
      .join(',')}}`;
  }
  return JSON.stringify(value);
}
export function checkSavedObservation(
  result: StockPieceDetail,
  command: CreateStockPiece | AppendStockPieceObservation,
  companyId: number,
  previous?: StockPieceDetail
) {
  checkObservationDetail(result, companyId);
  const instant = (value: string) => value.replace(/\.0+Z$/, 'Z');
  const mismatch =
    result.request_key !== command.request_key ||
    result.state !== command.state ||
    result.observation_number !== (previous ? previous.piece_version + 1 : 1) ||
    result.label !== (previous ? previous.label : 'label' in command ? command.label : undefined) ||
    (previous && result.piece_id !== previous.piece_id) ||
    result.reason !== command.reason ||
    result.observer_name !== command.observer_name ||
    instant(result.observed_at) !== instant(command.observed_at);
  if (mismatch) throw new Error('The save receipt does not match this command. Retry to recover the matching receipt.');
  if (command.state === 'RECORDED') {
    if (
      result.source_inventory_item_id !== command.source_inventory_item_id ||
      result.source_part_id !== command.source_part_id ||
      result.source_sha256 !== command.expected_source_sha256 ||
      canonicalJson(result.evidence) !== canonicalJson(command.evidence)
    )
      throw new Error('The saved measurement or source does not match the submitted evidence. Retry the same request.');
  } else if (
    !previous ||
    result.source_inventory_item_id !== previous.source_inventory_item_id ||
    result.source_part_id !== previous.source_part_id ||
    result.source_sha256 !== previous.source_sha256 ||
    result.payload_sha256 !== previous.payload_sha256 ||
    result.payload_bytes !== previous.payload_bytes ||
    canonicalJson(result.evidence) !== canonicalJson(previous.evidence) ||
    canonicalJson(result.source_snapshot) !== canonicalJson(previous.source_snapshot)
  ) {
    throw new Error('The withdrawal receipt changed the historical measurement or source. Retry the same request.');
  }
}
export function snapshotObject(snapshot: Record<string, unknown>, key: string): Record<string, unknown> {
  const value = snapshot[key];
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}
export function snapshotText(value: unknown): string {
  return typeof value === 'string' || typeof value === 'number'
    ? String(value)
    : typeof value === 'boolean'
      ? value
        ? 'Yes'
        : 'No'
      : 'Unknown';
}
export function sourceLabel(source: StockPieceSource): string {
  const part = snapshotObject(source.snapshot, 'part'),
    item = snapshotObject(source.snapshot, 'item');
  return `${snapshotText(part.part_number)} · ${snapshotText(part.name)} · lot ${snapshotText(item.lot_number)} · ${snapshotText(item.location)} · row ${source.inventory_item_id}`;
}
export const driftLabel = (status: StockPieceSummary['source_status']) =>
  status === 'changed'
    ? 'Source changed — review'
    : status === 'missing'
      ? 'Source missing — review'
      : 'No detected source change';
