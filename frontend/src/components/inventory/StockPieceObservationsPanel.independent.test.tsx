import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import api from '../../services/api';
import StockPieceObservationEditor from './StockPieceObservationEditor';
import StockPieceSourcePicker from './StockPieceSourcePicker';
import { STOCK_PIECE_ADVISORY } from '../../types/stockPiece';
import type {
  AppendStockPieceObservation,
  CreateStockPiece,
  StockPieceDetail,
  StockPieceEvidence,
  StockPiecePage,
  StockPieceSource,
} from '../../types/stockPiece';
import { emptyEvidence } from '../../validation/stockPiece';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getStockPieceSources: jest.fn(),
    createStockPiece: jest.fn(),
    appendStockPieceObservation: jest.fn(),
  },
}));
const mockApi = api as jest.Mocked<typeof api>;
const snapshot = {
  version: 1,
  item: { id: 4, company_id: 1, part_id: 8, location: 'SYNTHETIC-RACK', lot_number: 'SYNTHETIC-LOT' },
  part: { id: 8, company_id: 1, part_number: 'SYNTHETIC-PART', name: 'Reported source', unit_of_measure: 'sheets' },
  movement_watermark: {
    coverage: 'direct_item_and_unattributed_same_part',
    count: 2,
    max_id: 20,
    max_created_at: null,
  },
};
const source: StockPieceSource = {
  inventory_item_id: 4,
  part_id: 8,
  source_sha256: 'a'.repeat(64),
  snapshot,
  review_issues: [],
  advisory: STOCK_PIECE_ADVISORY,
};
const evidence: StockPieceEvidence = {
  ...emptyEvidence(),
  measurement_method: 'Synthetic measurement',
  geometry: {
    kind: 'polygon',
    outer: [
      { x: '0', y: '0' },
      { x: '4', y: '0' },
      { x: '0', y: '3' },
    ],
    holes: [],
  },
};
const previous: StockPieceDetail = {
  piece_id: 3,
  company_id: 1,
  label: 'SYNTHETIC-TAG',
  observation_number: 1,
  piece_version: 1,
  state: 'RECORDED',
  reason: 'Initial observation',
  observed_at: '2026-09-08T15:00:00Z',
  observer_name: 'Synthetic observer',
  created_at: '2026-09-08T16:00:00Z',
  created_by: 7,
  submitted_api_token_id: null,
  payload_schema_version: 1,
  payload_sha256: 'b'.repeat(64),
  payload_bytes: 500,
  source_inventory_item_id: 4,
  source_part_id: 8,
  source_sha256: source.source_sha256,
  source_status: 'unchanged',
  current_source_sha256: source.source_sha256,
  review_issues: [],
  advisory: STOCK_PIECE_ADVISORY,
  evidence,
  source_snapshot: snapshot,
  request_key: '11111111-1111-4111-8111-111111111111',
};
const page = (item: StockPieceSource): StockPiecePage<StockPieceSource> => ({
  company_id: 1,
  can_record: true,
  items: [item],
  page: 1,
  per_page: 10,
  total: 1,
});
function receipt(command: CreateStockPiece): StockPieceDetail {
  return {
    ...previous,
    label: command.label,
    reason: command.reason,
    observed_at: command.observed_at,
    observer_name: command.observer_name,
    evidence: command.evidence,
    request_key: command.request_key,
  };
}
function withdrawalReceipt(command: AppendStockPieceObservation): StockPieceDetail {
  return {
    ...previous,
    state: 'WITHDRAWN',
    observation_number: 2,
    piece_version: 2,
    reason: command.reason,
    observed_at: command.observed_at,
    observer_name: command.observer_name,
    request_key: command.request_key,
    source_status: 'missing',
    current_source_sha256: null,
  };
}
function fillObservation() {
  fireEvent.change(screen.getByLabelText(/Observer name/), { target: { value: 'Synthetic observer' } });
  fireEvent.change(screen.getByLabelText(/Observed at/), { target: { value: '2026-09-08T10:00' } });
  fireEvent.change(screen.getByLabelText(/Evidence \/ correction reason|Withdrawal reason/), {
    target: { value: 'Explicit observation at the rack' },
  });
}
async function fillNew() {
  fireEvent.change(screen.getByLabelText(/Physical piece label/), { target: { value: 'SYNTHETIC-TAG' } });
  fireEvent.click(await screen.findByRole('button', { name: /SYNTHETIC-PART/ }));
  fireEvent.change(screen.getByLabelText(/Measurement method/), { target: { value: 'Synthetic measurement' } });
  fillObservation();
}
beforeEach(() => {
  jest.clearAllMocks();
  Object.defineProperty(global.crypto, 'randomUUID', {
    configurable: true,
    value: jest.fn(() => '11111111-1111-4111-8111-111111111111'),
  });
  mockApi.getStockPieceSources.mockResolvedValue(page(source));
  mockApi.createStockPiece.mockImplementation(async command => receipt(command));
  mockApi.appendStockPieceObservation.mockImplementation(async (_id, command) => withdrawalReceipt(command));
});

it.each([
  [
    'measured outline',
    (value: StockPieceDetail) => ({
      ...value,
      evidence: { ...value.evidence, geometry: { kind: 'rectangle' as const, width: '99', height: '99' } },
    }),
  ],
  ['source Part', (value: StockPieceDetail) => ({ ...value, source_part_id: 88 })],
  ['source fingerprint', (value: StockPieceDetail) => ({ ...value, source_sha256: 'c'.repeat(64) })],
  ['physical label', (value: StockPieceDetail) => ({ ...value, label: 'OTHER-TAG' })],
] as const)('refuses a matching-key save receipt with different %s', async (_name, corrupt) => {
  const saved = jest.fn();
  mockApi.createStockPiece.mockImplementation(async command => corrupt(receipt(command)));
  render(<StockPieceObservationEditor companyId={1} canRecord onClose={jest.fn()} onSaved={saved} />);
  await fillNew();
  fireEvent.click(screen.getByRole('button', { name: 'Save observation' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(/receipt|source|match/i);
  expect(saved).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: 'Retry same request' })).toBeInTheDocument();
});

it('refuses a withdrawal receipt that replaces the preserved physical measurement', async () => {
  const saved = jest.fn();
  mockApi.appendStockPieceObservation.mockImplementation(async (_id, command) => ({
    ...withdrawalReceipt(command),
    evidence: { ...evidence, geometry: { kind: 'unknown' } },
  }));
  render(
    <StockPieceObservationEditor
      companyId={1}
      canRecord
      previous={previous}
      withdraw
      onClose={jest.fn()}
      onSaved={saved}
    />
  );
  fillObservation();
  fireEvent.click(screen.getByRole('button', { name: 'Record withdrawal' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(/receipt|match|evidence/i);
  expect(saved).not.toHaveBeenCalled();
  expect(mockApi.getStockPieceSources).not.toHaveBeenCalled();
});

it.each([
  ['foreign Part tenant', { ...snapshot, part: { ...snapshot.part, company_id: 2 } }],
  ['wrong item ID', { ...snapshot, item: { ...snapshot.item, id: 44 } }],
  ['mismatched item-Part relationship', { ...snapshot, item: { ...snapshot.item, part_id: 88 } }],
] as const)('does not offer a source with %s even when its page tenant matches', async (_name, corrupt) => {
  const select = jest.fn();
  const capability = jest.fn();
  mockApi.getStockPieceSources.mockResolvedValue(page({ ...source, snapshot: corrupt }));
  render(<StockPieceSourcePicker companyId={1} value={null} onChange={select} onCapability={capability} />);
  expect(await screen.findByRole('alert')).toHaveTextContent(/source|identity/i);
  expect(screen.queryByRole('button', { name: /SYNTHETIC-PART/ })).not.toBeInTheDocument();
  expect(capability).toHaveBeenLastCalledWith(false);
  expect(select).not.toHaveBeenCalled();
});

it('keeps one write in flight and aborts a late receipt after unmount', async () => {
  let resolve!: (value: StockPieceDetail) => void;
  mockApi.createStockPiece.mockImplementation(
    () =>
      new Promise(done => {
        resolve = done;
      })
  );
  const saved = jest.fn();
  const view = render(<StockPieceObservationEditor companyId={1} canRecord onClose={jest.fn()} onSaved={saved} />);
  await fillNew();
  const form = screen.getByRole('button', { name: 'Save observation' }).closest('form');
  if (!form) throw new Error('Missing observation form');
  fireEvent.submit(form);
  fireEvent.submit(form);
  await waitFor(() => expect(mockApi.createStockPiece).toHaveBeenCalledTimes(1));
  const [request, signal] = mockApi.createStockPiece.mock.calls[0];
  view.unmount();
  expect(signal?.aborted).toBe(true);
  await act(async () => resolve(receipt(request)));
  expect(saved).not.toHaveBeenCalled();
});

it('preserves the exact reported outline and interior cutout when correcting only observation metadata', async () => {
  const stored: StockPieceDetail = {
    ...previous,
    evidence: {
      ...evidence,
      geometry: {
        kind: 'polygon',
        outer: [
          { x: '0', y: '0' },
          { x: '4.000000001', y: '0' },
          { x: '0', y: '3' },
        ],
        holes: [
          [
            { x: '1', y: '1' },
            { x: '1.1', y: '1' },
            { x: '1', y: '1.1' },
          ],
        ],
      },
    },
  };
  const saved = jest.fn();
  mockApi.appendStockPieceObservation.mockImplementation(async (_id, command) => {
    if (command.state !== 'RECORDED') throw new Error('Expected an explicit correction');
    return {
      ...stored,
      observation_number: 2,
      piece_version: 2,
      evidence: command.evidence,
      request_key: command.request_key,
      reason: command.reason,
      observed_at: command.observed_at,
      observer_name: command.observer_name,
    };
  });
  render(<StockPieceObservationEditor companyId={1} canRecord previous={stored} onClose={jest.fn()} onSaved={saved} />);
  expect(screen.getByRole('button', { name: 'Save observation' })).toBeDisabled();
  fireEvent.click(await screen.findByRole('button', { name: /SYNTHETIC-PART/ }));
  fillObservation();
  fireEvent.click(screen.getByRole('button', { name: 'Save observation' }));
  await waitFor(() => expect(saved).toHaveBeenCalledTimes(1));
  expect(mockApi.appendStockPieceObservation.mock.calls[0][1]).toMatchObject({
    expected_version: 1,
    expected_source_sha256: source.source_sha256,
    evidence: stored.evidence,
  });
});
