/** Durable import review, reconciliation, and correction contracts through the actual page. */
import React from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import ImportCenter from './ImportCenter';
import { ImportBatch } from '../types/importBatch';

let mockRole = 'admin';
let mockCompanyId = 1;
jest.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: { id: 1, role: mockRole } }) }));
jest.mock('../context/CompanyContext', () => ({ useCompany: () => ({ currentCompany: { id: mockCompanyId } }) }));
jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getImportTemplates: jest.fn(),
    downloadImportTemplate: jest.fn(),
    listImportBatches: jest.fn(),
    getImportBatch: jest.fn(),
    prepareImportBatch: jest.fn(),
    commitImportBatch: jest.fn(),
    correctImportBatch: jest.fn(),
    downloadFailedImportRows: jest.fn(),
  },
}));
const mocked = api as jest.Mocked<typeof api>;
const file = new File(['part_number,name,part_type\nP-1,Plate,manufactured'], 'legacy.csv', { type: 'text/csv' });
const fixture = (extra: Partial<ImportBatch> = {}): ImportBatch => ({
  id: 1,
  entity: 'parts',
  filename: file.name,
  version: 1,
  created_at: '2026-09-07T12:00:00Z',
  updated_at: '2026-09-07T12:00:00Z',
  total_rows: 1,
  counts: { ready: 1 },
  created_records: 0,
  row_offset: 0,
  has_more_rows: false,
  requires_credentials: false,
  rows: [
    {
      row_key: 'row-1',
      source_row: 2,
      status: 'ready',
      data: { part_number: 'P-1', name: 'Plate', part_type: 'manufactured' },
    },
  ],
  ...extra,
});
const renderPage = (query = '?type=parts') =>
  render(
    <MemoryRouter initialEntries={[`/import-center${query}`]}>
      <ImportCenter />
    </MemoryRouter>
  );
const attach = () => fireEvent.change(screen.getByLabelText('Import file'), { target: { files: [file] } });
async function prepare() {
  attach();
  fireEvent.click(screen.getByRole('button', { name: 'Validate file (dry run)' }));
  await screen.findByRole('heading', { name: 'Import receipt #1' });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh receipt' })).toBeEnabled());
}
async function approve() {
  fireEvent.click(screen.getByRole('checkbox', { name: /I reviewed the ready rows/ }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Commit ready rows' })).toBeEnabled());
}
beforeAll(() => {
  Object.defineProperty(crypto, 'randomUUID', {
    configurable: true,
    value: () => '12345678-1234-4234-8234-123456789abc',
  });
  URL.createObjectURL = jest.fn(() => 'blob:review');
  URL.revokeObjectURL = jest.fn();
  jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
});
beforeEach(() => {
  jest.clearAllMocks();
  mockRole = 'admin';
  mockCompanyId = 1;
  mocked.getImportTemplates.mockResolvedValue({ templates: [] });
  mocked.listImportBatches.mockResolvedValue({ batches: [], has_more: false });
  mocked.getImportBatch.mockResolvedValue(fixture());
  mocked.prepareImportBatch.mockResolvedValue(fixture());
});

it('saves a dry-run receipt and requires explicit review before committing its version', async () => {
  mocked.commitImportBatch.mockResolvedValue(
    fixture({
      version: 2,
      counts: { created: 1 },
      created_records: 1,
      rows: [{ ...fixture().rows[0], status: 'created', result: { record_id: 11, entity: 'parts' } }],
    })
  );
  renderPage();
  await prepare();
  expect(mocked.prepareImportBatch).toHaveBeenCalledWith('parts', file, expect.any(String), '');
  expect(mocked.commitImportBatch).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: 'Commit ready rows' })).toBeDisabled();
  await approve();
  fireEvent.click(screen.getByRole('button', { name: 'Commit ready rows' }));
  expect(await screen.findByRole('link', { name: 'Open created record' })).toHaveAttribute('href', '/parts/11');
  expect(mocked.commitImportBatch).toHaveBeenCalledTimes(1);
  expect(mocked.commitImportBatch).toHaveBeenCalledWith(1, 1, null, '');
});

it('retains the same prepare key after a lost response', async () => {
  mocked.prepareImportBatch.mockRejectedValueOnce(new Error('Connection lost')).mockResolvedValueOnce(fixture());
  renderPage();
  attach();
  fireEvent.click(screen.getByRole('button', { name: 'Validate file (dry run)' }));
  await screen.findByText('Connection lost');
  fireEvent.click(screen.getByRole('button', { name: 'Validate file (dry run)' }));
  await screen.findByRole('heading', { name: 'Import receipt #1' });
  expect(mocked.prepareImportBatch.mock.calls[0][2]).toBe(mocked.prepareImportBatch.mock.calls[1][2]);
});

it('requires reconciliation after a lost commit response and never blindly resends', async () => {
  mocked.commitImportBatch.mockRejectedValueOnce(new Error('Connection lost'));
  renderPage();
  await prepare();
  await approve();
  fireEvent.click(screen.getByRole('button', { name: 'Commit ready rows' }));
  await screen.findByText(/Refresh the receipt to see which rows committed/);
  expect(screen.getByRole('button', { name: 'Commit ready rows' })).toBeDisabled();
  mocked.getImportBatch.mockResolvedValue(
    fixture({ version: 2, counts: { created: 1 }, created_records: 1, rows: [] })
  );
  fireEvent.click(screen.getByRole('button', { name: 'Refresh receipt' }));
  await screen.findByText(/1 records created/);
  expect(mocked.commitImportBatch).toHaveBeenCalledTimes(1);
});

it('opens a saved receipt after reload without writing', async () => {
  renderPage('?type=parts&batch=1');
  await screen.findByRole('heading', { name: 'Import receipt #1' });
  expect(mocked.prepareImportBatch).not.toHaveBeenCalled();
  expect(mocked.commitImportBatch).not.toHaveBeenCalled();
});

it('validates failed-row corrections separately from the created records', async () => {
  mocked.getImportBatch.mockResolvedValue(
    fixture({ counts: { invalid: 1 }, rows: [{ ...fixture().rows[0], status: 'invalid', error: 'Fix quantity' }] })
  );
  mocked.correctImportBatch.mockResolvedValue(fixture({ version: 2 }));
  renderPage('?type=parts&batch=1');
  await screen.findByText('Fix quantity');
  expect(screen.getByRole('button', { name: 'Commit ready rows' })).toBeDisabled();
  const correction = new File(['_import_row_id,quantity\nrow-1,2'], 'fixed.csv');
  fireEvent.change(screen.getByLabelText('Corrected failed-row file'), { target: { files: [correction] } });
  fireEvent.click(screen.getByRole('button', { name: 'Validate corrections' }));
  await waitFor(() => expect(mocked.correctImportBatch).toHaveBeenCalledWith(1, 1, correction, ''));
  await screen.findByText(/1 ready/);
  expect(mocked.commitImportBatch).not.toHaveBeenCalled();
});

it('keeps failed history loading distinct from an empty history', async () => {
  mocked.listImportBatches.mockRejectedValueOnce(new Error('Unavailable'));
  renderPage();
  await screen.findByText(/Batch history is unavailable/);
  expect(screen.queryByText(/No saved batches/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Retry history' }));
  await screen.findByText(/No saved batches/);
});

it('keeps specialized BOM imports on their existing wizard', async () => {
  renderPage('?type=boms');
  expect(screen.getByRole('link', { name: 'Open Bill of Materials' })).toHaveAttribute('href', '/bom');
  expect(screen.queryByLabelText('Import file')).not.toBeInTheDocument();
  await waitFor(() => expect(mocked.getImportTemplates).toHaveBeenCalled());
});

it('uses the server template and prevents supervisors from importing employees', async () => {
  mockRole = 'supervisor';
  mocked.downloadImportTemplate.mockResolvedValue({ blob: new Blob(['template']), filename: 'users.xlsx' });
  renderPage('?type=employees');
  expect(screen.getByLabelText('Import file')).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Download template (.xlsx)' }));
  await waitFor(() => expect(mocked.downloadImportTemplate).toHaveBeenCalledWith('users'));
});

it('clears stale company receipts and drops the old read response', async () => {
  let finish!: (batch: ImportBatch) => void;
  mocked.getImportBatch
    .mockImplementationOnce(
      () =>
        new Promise(resolve => {
          finish = resolve;
        })
    )
    .mockRejectedValueOnce(new Error('Receipt is not in this company'));
  const view = renderPage('?type=parts&batch=1');
  await waitFor(() => expect(mocked.getImportBatch).toHaveBeenCalledTimes(1));
  mockCompanyId = 2;
  view.rerender(
    <MemoryRouter>
      <ImportCenter />
    </MemoryRouter>
  );
  await screen.findByText('Receipt is not in this company');
  await act(async () => finish(fixture()));
  expect(screen.queryByRole('heading', { name: 'Import receipt #1' })).not.toBeInTheDocument();
  expect(mocked.commitImportBatch).not.toHaveBeenCalled();
});

it('stops queued chunks immediately on token replacement before CompanyContext changes', async () => {
  let finish!: (batch: ImportBatch) => void;
  mocked.commitImportBatch.mockImplementationOnce(
    () =>
      new Promise(resolve => {
        finish = resolve;
      })
  );
  renderPage();
  await prepare();
  await approve();
  fireEvent.click(screen.getByRole('button', { name: 'Commit ready rows' }));
  expect(mocked.commitImportBatch).toHaveBeenCalledTimes(1);
  mocked.getImportBatch.mockRejectedValueOnce(new Error('Receipt is not in this company'));
  act(() => window.dispatchEvent(new Event('werco:auth-token-changed')));
  await screen.findByText('Receipt is not in this company');
  await act(async () => finish(fixture({ version: 2, counts: { ready: 1, created: 25 }, created_records: 25 })));
  expect(mocked.commitImportBatch).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole('heading', { name: 'Import receipt #1' })).not.toBeInTheDocument();
});
