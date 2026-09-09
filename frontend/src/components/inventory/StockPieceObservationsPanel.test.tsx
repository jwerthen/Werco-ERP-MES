import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import StockPieceObservationsPanel from './StockPieceObservationsPanel';
import StockPieceObservationEditor from './StockPieceObservationEditor';
import { StockPieceGeometryPreview } from './StockPieceGeometry';
import { STOCK_PIECE_ADVISORY } from '../../types/stockPiece';
import type {
  CreateStockPiece,
  StockPieceDetail,
  StockPiecePage,
  StockPieceSource,
  StockPieceSummary,
} from '../../types/stockPiece';
import { emptyEvidence } from '../../validation/stockPiece';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getStockPieces: jest.fn(),
    getStockPieceSources: jest.fn(),
    getStockPieceHistory: jest.fn(),
    getStockPieceObservation: jest.fn(),
    createStockPiece: jest.fn(),
    appendStockPieceObservation: jest.fn(),
  },
}));
let mockCompanyId = 1;
jest.mock('../../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 7, company_id: 1, role: 'supervisor' } }),
}));
jest.mock('../../context/CompanyContext', () => ({ useCompany: () => ({ currentCompany: { id: mockCompanyId } }) }));
const mockApi = api as jest.Mocked<typeof api>;
const snapshot = {
  version: 1,
  item: {
    id: 4,
    company_id: 1,
    part_id: 8,
    lot_number: 'L1',
    location: 'A1',
    warehouse: 'MAIN',
    quantity_on_hand: '5',
    status: 'available',
  },
  part: { id: 8, company_id: 1, part_number: 'A36', name: 'Steel', unit_of_measure: 'sheets' },
  movement_watermark: {
    coverage: 'direct_item_and_unattributed_same_part',
    count: 2,
    max_id: 20,
    max_created_at: '2026-09-08T12:00:00Z',
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
const evidence = {
  ...emptyEvidence(),
  measurement_method: 'Tape',
  geometry: { kind: 'rectangle' as const, width: '12', height: '8' },
};
const detail: StockPieceDetail = {
  piece_id: 3,
  company_id: 1,
  label: 'TAG-1',
  observation_number: 1,
  piece_version: 1,
  state: 'RECORDED',
  reason: 'Initial measured observation',
  observed_at: '2026-09-08T15:00:00Z',
  observer_name: 'Pat',
  created_at: '2026-09-08T16:00:00Z',
  created_by: 7,
  submitted_api_token_id: null,
  payload_schema_version: 1,
  payload_sha256: 'b'.repeat(64),
  payload_bytes: 1000,
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
function page<T>(items: T[], can_record = true, company_id = 1): StockPiecePage<T> {
  return { company_id, can_record, items, page: 1, per_page: 20, total: items.length };
}
function receipt(command: CreateStockPiece): StockPieceDetail {
  return {
    ...detail,
    label: command.label,
    reason: command.reason,
    observer_name: command.observer_name,
    observed_at: command.observed_at,
    evidence: command.evidence,
    request_key: command.request_key,
  };
}
beforeEach(() => {
  jest.clearAllMocks();
  mockCompanyId = 1;
  Object.defineProperty(global.crypto, 'randomUUID', {
    configurable: true,
    value: jest.fn(() => '11111111-1111-4111-8111-111111111111'),
  });
  mockApi.getStockPieces.mockResolvedValue(page([]));
  mockApi.getStockPieceSources.mockResolvedValue(page([source]));
  mockApi.getStockPieceHistory.mockResolvedValue(page([detail]));
  mockApi.getStockPieceObservation.mockResolvedValue(detail);
  mockApi.createStockPiece.mockImplementation(async command => receipt(command));
});
function fillIdentity() {
  fireEvent.change(screen.getByLabelText(/Observer name/), { target: { value: 'Pat' } });
  fireEvent.change(screen.getByLabelText(/Observed at/), { target: { value: '2026-09-08T10:00' } });
  fireEvent.change(screen.getByLabelText(/Evidence \/ correction reason|Withdrawal reason/), {
    target: { value: 'Measured at rack A1' },
  });
}
async function fillNew() {
  fireEvent.change(screen.getByLabelText(/Physical piece label/), { target: { value: 'TAG-1' } });
  fireEvent.click(await screen.findByRole('button', { name: /A36 · Steel/ }));
  fireEvent.change(screen.getByLabelText('Reported shape'), { target: { value: 'rectangle' } });
  fireEvent.change(screen.getByLabelText(/Horizontal X/), { target: { value: '12.00' } });
  fireEvent.change(screen.getByLabelText(/Vertical Y/), { target: { value: '1 1/8' } });
  fireEvent.change(screen.getByLabelText(/Measurement method/), { target: { value: 'Tape' } });
  fillIdentity();
}

it('uses server write capability and never presents observations as available inventory', async () => {
  mockApi.getStockPieces.mockResolvedValue(page([detail], false));
  mockApi.getStockPieceHistory.mockResolvedValue(page([detail], false));
  render(
    <MemoryRouter>
      <StockPieceObservationsPanel />
    </MemoryRouter>
  );
  expect(await screen.findByRole('button', { name: 'TAG-1' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Record piece observation' })).not.toBeInTheDocument();
  expect(screen.getByText(STOCK_PIECE_ADVISORY)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'TAG-1' }));
  expect(await screen.findByRole('dialog', { name: 'Piece observation history' })).toBeInTheDocument();
  await screen.findByText('Immutable history');
  expect(screen.queryByRole('button', { name: 'Record correction' })).not.toBeInTheDocument();
});

it('records explicit source and canonical inches, retaining an exact request for network retry', async () => {
  const saved = jest.fn();
  mockApi.createStockPiece.mockRejectedValueOnce(new Error('Connection interrupted'));
  render(<StockPieceObservationEditor companyId={1} canRecord onClose={jest.fn()} onSaved={saved} />);
  expect(screen.getByRole('button', { name: 'Save observation' })).toBeDisabled();
  await fillNew();
  fireEvent.click(screen.getByRole('button', { name: 'Save observation' }));
  await screen.findByText('Connection interrupted');
  const original = mockApi.createStockPiece.mock.calls[0][0];
  expect(original).toMatchObject({
    expected_company_id: 1,
    source_inventory_item_id: 4,
    source_part_id: 8,
    expected_source_sha256: source.source_sha256,
    observed_at: '2026-09-08T15:00:00.000Z',
    evidence: { geometry: { kind: 'rectangle', width: '12', height: '1.125' }, grade: null, thickness: null },
  });
  expect(screen.getByLabelText(/Physical piece label/)).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Retry same request' }));
  await waitFor(() => expect(saved).toHaveBeenCalledTimes(1));
  expect(mockApi.createStockPiece.mock.calls[1][0]).toEqual(original);
});

it('retains historical CAS and offers source review after a conflict', async () => {
  const older = { ...detail, observation_number: 2, piece_version: 2 };
  mockApi.appendStockPieceObservation.mockRejectedValue({
    response: { status: 409, data: { detail: 'A newer observation exists' } },
  });
  render(
    <StockPieceObservationEditor companyId={1} canRecord previous={older} onClose={jest.fn()} onSaved={jest.fn()} />
  );
  fireEvent.click(await screen.findByRole('button', { name: /A36 · Steel/ }));
  fillIdentity();
  fireEvent.click(screen.getByRole('button', { name: 'Save observation' }));
  await screen.findByText('A newer observation exists');
  expect(mockApi.appendStockPieceObservation.mock.calls[0][1]).toMatchObject({ expected_version: 2 });
  expect(screen.getByRole('button', { name: 'Review and edit rejected request' })).toBeInTheDocument();
});

it('withdraws without requesting live source or resubmitting measurement evidence', async () => {
  const saved = jest.fn();
  mockApi.appendStockPieceObservation.mockImplementation(async (_id, command) => ({
    ...detail,
    observation_number: 2,
    piece_version: 2,
    state: 'WITHDRAWN',
    source_status: 'missing',
    current_source_sha256: null,
    reason: command.reason,
    observed_at: command.observed_at,
    observer_name: command.observer_name,
    request_key: command.request_key,
  }));
  render(
    <StockPieceObservationEditor
      companyId={1}
      canRecord
      previous={{ ...detail, source_status: 'missing' }}
      withdraw
      onClose={jest.fn()}
      onSaved={saved}
    />
  );
  fillIdentity();
  fireEvent.click(screen.getByRole('button', { name: 'Record withdrawal' }));
  await waitFor(() => expect(saved).toHaveBeenCalledTimes(1));
  expect(mockApi.getStockPieceSources).not.toHaveBeenCalled();
  expect(mockApi.appendStockPieceObservation.mock.calls[0][1]).toMatchObject({
    state: 'WITHDRAWN',
    expected_version: 1,
  });
  expect(mockApi.appendStockPieceObservation.mock.calls[0][1]).not.toHaveProperty('evidence');
});

it('aborts old company reads and rejects a mismatched-company source page', async () => {
  let finish!: (value: StockPiecePage<StockPieceSummary>) => void;
  mockApi.getStockPieces
    .mockImplementationOnce(
      () =>
        new Promise(resolve => {
          finish = resolve;
        })
    )
    .mockResolvedValueOnce(page([], true, 2));
  const view = render(
    <MemoryRouter>
      <StockPieceObservationsPanel />
    </MemoryRouter>
  );
  const signal = mockApi.getStockPieces.mock.calls[0][1];
  mockCompanyId = 2;
  view.rerender(
    <MemoryRouter>
      <StockPieceObservationsPanel />
    </MemoryRouter>
  );
  await act(async () => finish(page([detail])));
  expect(signal?.aborted).toBe(true);
  expect(screen.queryByRole('button', { name: 'TAG-1' })).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole('button', { name: 'Record piece observation' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('another company');
  expect(screen.queryByRole('button', { name: /A36 · Steel/ })).not.toBeInTheDocument();
});

it('shows the real polygon and holes, including invalid outlying hole evidence in preview extents', () => {
  const shape = {
    kind: 'polygon' as const,
    outer: [
      { x: '0', y: '0' },
      { x: '12', y: '0' },
      { x: '0', y: '8' },
    ],
    holes: [
      [
        { x: '20', y: '20' },
        { x: '21', y: '20' },
        { x: '20', y: '21' },
      ],
    ],
  };
  render(<StockPieceGeometryPreview shape={shape} zones={[]} />);
  const svg = screen.getByRole('img');
  expect(svg.querySelector('rect')).toBeNull();
  expect(svg.querySelector('path')).toHaveAttribute('fill-rule', 'evenodd');
  expect(svg.querySelector('path')?.getAttribute('d')).toContain('M20 20');
  expect(Number(svg.getAttribute('viewBox')?.split(' ')[2])).toBeGreaterThan(21);
});

it('uses the shared unsaved-change guard for cancelling edited observations', async () => {
  const close = jest.fn();
  const confirm = jest.spyOn(window, 'confirm').mockReturnValue(false);
  render(<StockPieceObservationEditor companyId={1} canRecord onClose={close} onSaved={jest.fn()} />);
  fireEvent.change(screen.getByLabelText(/Physical piece label/), { target: { value: 'Unsaved tag' } });
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));
  expect(confirm).toHaveBeenCalled();
  expect(close).not.toHaveBeenCalled();
  confirm.mockRestore();
  await act(async () => undefined);
});
