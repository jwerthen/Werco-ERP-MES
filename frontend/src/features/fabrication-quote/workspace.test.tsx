import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import FabricationQuoting from '../../pages/FabricationQuoting';
import { fabricationQuoteApi } from './api';
import { emptyPlan, newEvidence } from './types';
import type { QuoteRecord } from './types';

jest.mock('./api', () => ({ fabricationQuoteApi: { list: jest.fn(), get: jest.fn(), capabilities: jest.fn(), save: jest.fn(), create: jest.fn(), calculate: jest.fn(), approve: jest.fn(), profiles: jest.fn() } }));
jest.mock('../../services/api', () => ({ __esModule: true, default: { getCustomerNames: jest.fn().mockResolvedValue([]) } }));
jest.mock('../../components/ui/PdfPreview', () => ({ __esModule: true, default: () => <div>PDF preview</div> }));
jest.mock('./StepViewer', () => ({ __esModule: true, default: () => <div>STEP preview</div> }));
const mocked = fabricationQuoteApi as jest.Mocked<typeof fabricationQuoteApi>;
const record = (): QuoteRecord => ({ id: 42, title: 'Assembly A', customer_id: null, revision: 4, status: 'draft', files: [], plan: { ...emptyPlan(), parts: [{ id: 'p', name: 'Assembly', make_or_buy: 'make', costing_complete: false, purchase_unit_cost: null, evidence: newEvidence() }], roots: [{ part_id: 'p', quantity: '10' }] }, calculation: { engine_version: 'test', input_hash: 'hash', can_approve: true, issues: [], totals: { total_cost: '5' } } });
beforeEach(() => { jest.clearAllMocks(); const saved = record(); mocked.list.mockResolvedValue({ items: [saved], total: 1 }); mocked.get.mockResolvedValue(saved); mocked.capabilities.mockResolvedValue({ can_write: true }); mocked.profiles.mockResolvedValue({ items: [], total: 0 }); });
const mount = () => render(<MemoryRouter initialEntries={['/fabrication-quotes?id=42']}><FabricationQuoting /></MemoryRouter>);

test('unsaved quantities disable approval and preview keeps saved source context', async () => {
  mocked.calculate.mockResolvedValue(record().calculation!); mount(); await waitFor(() => expect(screen.getByLabelText('Quote title')).toHaveValue('Assembly A'));
  fireEvent.change(screen.getByLabelText('Customer quantity'), { target: { value: '20.125' } }); fireEvent.click(screen.getByRole('button', { name: 'Calculate' }));
  await waitFor(() => expect(mocked.calculate).toHaveBeenCalledWith(expect.objectContaining({ roots: [{ part_id: 'p', quantity: '20.125' }] }), 42));
  fireEvent.change(await screen.findByLabelText('Estimator release note'), { target: { value: 'Reviewed package' } }); expect(screen.getByRole('button', { name: 'Approve revision' })).toBeDisabled(); expect(mocked.approve).not.toHaveBeenCalled();
});

test('revision conflicts retain local decimal edits and prevent another save', async () => {
  mocked.save.mockRejectedValue({ response: { status: 409 } }); mount(); await waitFor(() => expect(screen.getByLabelText('Quote title')).toHaveValue('Assembly A'));
  fireEvent.change(screen.getByLabelText('Customer quantity'), { target: { value: '250.000000001' } }); fireEvent.click(screen.getByRole('button', { name: 'Save draft' }));
  await screen.findByText(/This quote changed on the server/); expect(screen.getByLabelText('Customer quantity')).toHaveValue('250.000000001'); expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled();
  expect(mocked.save).toHaveBeenCalledWith(42, 4, expect.objectContaining({ plan: expect.objectContaining({ roots: [{ part_id: 'p', quantity: '250.000000001' }] }) }));
});

test('effective read-only capability disables creation, edits, save and approval', async () => {
  mocked.capabilities.mockResolvedValue({ can_write: false }); mount(); await screen.findByText(/Read-only access/);
  expect(screen.getByRole('button', { name: 'New quote' })).toBeDisabled(); expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled(); expect(screen.getByLabelText('Customer quantity')).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Review' })); expect(screen.getByLabelText('Estimator release note')).toBeDisabled(); expect(screen.getByRole('button', { name: 'Approve revision' })).toBeDisabled();
});
