import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import BuyerPdfExport from './BuyerPdfExport';
import { NestingPortalContext } from './PortalContext';
import { createBlankProject } from './lib/quote-project';
import { compareSheets, createBlankQuote } from './lib/quoting';
import { rect } from './lib/nesting';
import type { BuyerPdfInputs } from './lib/buyer-pdf-types';
import api from '../../services/api';

jest.mock('../../services/api', () => ({ __esModule: true, default: { generateNestingBuyerPdf: jest.fn() } }));
const generate = jest.mocked(api.generateNestingBuyerPdf);

function fixture(): BuyerPdfInputs {
  const quote = {
    ...createBlankQuote(),
    options: [{ id: 'sheet', width: 203.2, height: 101.6, price: null, enabled: true }],
    parts: [{ id: 'part', name: 'Plate', quantity: 1, rotate: true, color: 0, loops: [rect(25.4, 25.4)] }],
  };
  return {
    project: createBlankProject(quote),
    companyId: 1,
    remnantInput: null,
    stages: [],
    comparisons: { 'group-1': { signature: JSON.stringify(quote), comparison: compareSheets(quote) } },
  };
}

beforeEach(() => {
  jest.clearAllMocks();
  jest.spyOn(URL, 'createObjectURL').mockReturnValue('blob:independent-buyer');
  jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
});
afterEach(() => jest.restoreAllMocks());

test.each(['company', 'user', 'comparison', 'permission', 'new-search'] as const)(
  '%s changes abort and permanently discard a pending PDF, even if the old context returns',
  async change => {
    let finish!: (value: Blob) => void;
    generate.mockImplementationOnce(
      () =>
        new Promise(resolve => {
          finish = resolve;
        })
    );
    const inputs = fixture();
    const original = { inputs, estimatorId: 7, disabled: false, canPlanRemnants: true };
    const view = render(<BuyerPdfExport {...original} />, {
      wrapper: ({ children }) => (
        <NestingPortalContext.Provider value={document.body}>{children}</NestingPortalContext.Provider>
      ),
    });
    fireEvent.click(screen.getByRole('button', { name: 'Export buyer PDF' }));
    await screen.findByRole('dialog', { name: 'Buyer material plan PDF' });
    fireEvent.click(screen.getByRole('button', { name: 'Download buyer PDF' }));
    await waitFor(() => expect(generate).toHaveBeenCalledTimes(1));
    const signal = generate.mock.calls[0][1]!;
    const changed = { ...original };
    if (change === 'company') changed.inputs = { ...inputs, companyId: 2 };
    if (change === 'user') changed.estimatorId = 8;
    if (change === 'comparison') changed.inputs = { ...inputs, comparisons: { ...inputs.comparisons } };
    if (change === 'permission') changed.canPlanRemnants = false;
    if (change === 'new-search') changed.disabled = true;
    view.rerender(<BuyerPdfExport {...changed} />);
    expect(signal.aborted).toBe(true);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    view.rerender(<BuyerPdfExport {...original} />);
    await act(async () => finish(new Blob(['%PDF-1.7\nLate result'], { type: 'application/pdf' })));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(URL.createObjectURL).not.toHaveBeenCalled();
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  }
);

test('a forced metadata change while disabled discards a late PDF instead of attaching the wrong notes', async () => {
  let finish!: (value: Blob) => void;
  generate.mockImplementationOnce(
    () =>
      new Promise(resolve => {
        finish = resolve;
      })
  );
  render(<BuyerPdfExport inputs={fixture()} estimatorId={7} disabled={false} canPlanRemnants />, {
    wrapper: ({ children }) => (
      <NestingPortalContext.Provider value={document.body}>{children}</NestingPortalContext.Provider>
    ),
  });
  fireEvent.click(screen.getByRole('button', { name: 'Export buyer PDF' }));
  await screen.findByRole('dialog');
  const notes = screen.getByLabelText('Buyer notes (optional)');
  fireEvent.change(notes, { target: { value: 'Original buyer notes' } });
  fireEvent.click(screen.getByRole('button', { name: 'Download buyer PDF' }));
  await waitFor(() => expect(generate).toHaveBeenCalledTimes(1));
  expect(notes).toBeDisabled();
  expect(generate.mock.calls[0][0].notes).toBe('Original buyer notes');
  fireEvent.change(notes, { target: { value: 'Changed while awaiting response' } });
  await act(async () => finish(new Blob(['%PDF-1.7\nOld notes'], { type: 'application/pdf' })));
  expect(URL.createObjectURL).not.toHaveBeenCalled();
  expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  expect(screen.queryByText('Buyer PDF downloaded. No order was submitted.')).not.toBeInTheDocument();
});
