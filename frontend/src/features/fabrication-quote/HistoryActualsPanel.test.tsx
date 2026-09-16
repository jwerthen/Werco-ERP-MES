import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { HistoryActualsPanel } from './HistoryActualsPanel';
import { fabricationQuoteApi } from './api';
import { emptyPlan, newEvidence, newRecipe } from './types';
import type { QuoteRecord } from './types';

jest.mock('./api', () => ({ fabricationQuoteApi: { revisions: jest.fn(), actuals: jest.fn(), revision: jest.fn(), addActual: jest.fn() } }));
jest.mock('./SourcesPanel', () => ({ downloadJson: jest.fn(), objectValue: (value: unknown) => value && typeof value === 'object' ? value : {} }));
const mocked = fabricationQuoteApi as jest.Mocked<typeof fabricationQuoteApi>;
const record: QuoteRecord = { id: 7, title: 'Package', customer_id: null, revision: 5, status: 'draft', files: [], calculation: null, plan: { ...emptyPlan(), operations: [{ id: 'current-only', part_id: 'p', name: 'Current draft operation', process: 'manual', setup_basis: 'per_quote', run_basis: 'per_unit', batch_size: '1', setup_labor_seconds: null, setup_machine_seconds: null, labor_rate_per_hour: null, machine_rate_per_hour: null, consumables_cost_per_run: null, outside_cost_per_run: null, recipe: newRecipe('manual'), evidence: newEvidence() }] } };
beforeEach(() => { jest.clearAllMocks(); mocked.actuals.mockResolvedValue({ items: [] }); mocked.revisions.mockResolvedValue({ items: [{ revision: 5, action: 'save', note: '', created_at: '2026-01-01', content_sha256: 'hash' }, { revision: 3, action: 'approve', note: '', created_at: '2026-01-01', content_sha256: 'hash' }] }); mocked.revision.mockResolvedValue({ plan: { operations: [{ id: 'historic', name: 'Historical laser' }, { id: 'unreachable', name: 'Unused operation' }] }, calculation: { operation_lines: [{ id: 'historic' }] } }); });

test('actuals use only operations from the selected approved calculation and submit that revision', async () => {
  render(<HistoryActualsPanel record={record} canWrite />);
  const select = await screen.findByLabelText('Observed operation'); await waitFor(() => expect(within(select).getByRole('option', { name: 'Historical laser' })).toBeInTheDocument());
  expect(within(select).queryByRole('option', { name: 'Current draft operation' })).not.toBeInTheDocument(); expect(within(select).queryByRole('option', { name: 'Unused operation' })).not.toBeInTheDocument();
  const revision = screen.getByLabelText('Observed quote revision'); expect(within(revision).getAllByRole('option')).toHaveLength(1); expect(revision).toHaveValue('3');
  fireEvent.change(screen.getByLabelText('Observation date'), { target: { value: '2026-01-01' } }); fireEvent.change(screen.getByLabelText('Good quantity observed'), { target: { value: '10' } }); fireEvent.change(screen.getByLabelText('Scrap quantity observed'), { target: { value: '0' } }); fireEvent.change(screen.getByLabelText('Observation source'), { target: { value: 'Measured trial' } }); fireEvent.change(screen.getByLabelText('Observation note'), { target: { value: 'Partial timing' } });
  fireEvent.click(screen.getByRole('button', { name: 'Record observation' }));
  await waitFor(() => expect(mocked.addActual).toHaveBeenCalledWith(7, expect.objectContaining({ quote_revision: 3, operation_id: 'historic', good_quantity: '10', scrap_quantity: '0', run_labor_seconds: null })));
});

test('no approved revision exposes no actuals entry form', async () => {
  mocked.revisions.mockResolvedValue({ items: [{ revision: 5, action: 'save', note: '', created_at: '2026-01-01', content_sha256: 'hash' }] });
  render(<HistoryActualsPanel record={record} canWrite />); await screen.findByText('Approve an estimate revision before recording actual work.'); expect(screen.queryByLabelText('Observed operation')).not.toBeInTheDocument(); expect(mocked.revision).not.toHaveBeenCalled();
});
