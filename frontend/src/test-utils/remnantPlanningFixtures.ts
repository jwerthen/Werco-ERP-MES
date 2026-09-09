import { createBlankQuote, quoteToFile } from '../features/nesting/lib/quoting';
import { rect } from '../features/nesting/lib/nesting';
import { inToMm } from '../features/nesting/lib/units';
import { canonicalJSON, sha256 } from '../features/nesting/lib/provenance';
import { remnantEvidenceHash } from '../features/nesting/lib/remnant-evidence';
import { emptyEvidence } from '../validation/stockPiece';
import { REMNANT_PLANNING_ADVISORY, type RemnantResolution, type RemnantSnapshot } from '../types/remnantPlanning';
import { STOCK_PIECE_ADVISORY, type StockPieceSummary, type StockPiecePage } from '../types/stockPiece';

export async function remnantPlanningFixture() {
  const quote = quoteToFile({
    ...createBlankQuote(),
    thickness: inToMm(0.125),
    margin: inToMm(0.375),
    parts: [{ id: 'plate', name: 'Synthetic plate', quantity: 2, rotate: true, color: 0, loops: [rect(25.4, 12.7)] }],
  });
  const evidence = {
    ...emptyEvidence(),
    measurement_method: 'Synthetic coordinate measurement',
    thickness: '0.125',
    grade: 'A36',
    geometry: {
      kind: 'polygon' as const,
      outer: [
        ['-0.000000001', '0'],
        ['12', '0'],
        ['12', '3'],
        ['6', '3'],
        ['6', '8'],
        ['-0.000000001', '8'],
      ].map(([x, y]) => ({ x, y })),
      holes: [
        [
          ['1', '1'],
          ['2', '1'],
          ['2', '2'],
          ['1', '2'],
        ].map(([x, y]) => ({ x, y })),
      ],
    },
  };
  const snapshot: RemnantSnapshot = {
    version: 1,
    companyId: 2,
    pieceId: 41,
    label: 'Café L-piece',
    observationNumber: 2,
    state: 'RECORDED',
    observedAt: '2026-09-08T12:00:00Z',
    observerName: 'Synthetic observer',
    reason: 'Measured remaining outline',
    createdAt: '2026-09-08T12:05:00Z',
    createdBy: 7,
    submittedApiTokenId: null,
    payloadSchemaVersion: 1,
    payloadSha256: await sha256(canonicalJSON(evidence)),
    payloadBytes: new TextEncoder().encode(canonicalJSON(evidence)).length,
    evidence,
    sourceInventoryItemId: 23,
    sourcePartId: 31,
    sourceSha256: 'a'.repeat(64),
    sourceEvidence: {
      version: 1,
      item: {
        id: 23,
        company_id: 2,
        part_id: 31,
        location: 'Rack 1',
        warehouse: null,
        lot_number: 'LOT-1',
        serial_number: null,
        received_date: null,
        supplier_id: null,
        po_number: null,
        cert_number: null,
        heat_lot: null,
        expiration_date: null,
        status: 'available',
        is_active: true,
        updated_at: null,
      },
      part: {
        id: 31,
        company_id: 2,
        part_number: 'SYNTHETIC-STOCK',
        revision: null,
        name: 'Synthetic source',
        part_type: 'raw_material',
        unit_of_measure: 'SHEET',
        is_active: true,
        is_deleted: false,
        updated_at: null,
      },
      movement_watermark: {
        coverage: 'direct_item_and_unattributed_same_part',
        count: 0,
        max_id: null,
        max_created_at: null,
      },
    },
  };
  const resolution: RemnantResolution = {
    company_id: 2,
    snapshot,
    snapshot_sha256: await remnantEvidenceHash(snapshot),
    latest_observation_number: 2,
    source_status: 'unchanged',
    current_source_sha256: snapshot.sourceSha256,
    checked_at: '2026-09-08T12:10:00Z',
    review_issues: ['Material and availability remain unverified.'],
    advisory: REMNANT_PLANNING_ADVISORY,
  };
  const summary: StockPieceSummary = {
    piece_id: 41,
    company_id: 2,
    label: snapshot.label,
    observation_number: 2,
    piece_version: 2,
    state: 'RECORDED',
    reason: snapshot.reason,
    observed_at: snapshot.observedAt,
    observer_name: snapshot.observerName,
    created_at: snapshot.createdAt,
    created_by: 7,
    submitted_api_token_id: null,
    payload_schema_version: 1,
    payload_sha256: snapshot.payloadSha256,
    payload_bytes: snapshot.payloadBytes,
    source_inventory_item_id: 23,
    source_part_id: 31,
    source_sha256: snapshot.sourceSha256,
    source_status: 'unchanged',
    current_source_sha256: snapshot.sourceSha256,
    review_issues: [],
    advisory: STOCK_PIECE_ADVISORY,
  };
  const page: StockPiecePage<StockPieceSummary> = {
    company_id: 2,
    can_record: false,
    items: [summary],
    total: 1,
    page: 1,
    per_page: 20,
  };
  return { quote, snapshot, resolution, summary, page, groupId: 'g1' };
}
