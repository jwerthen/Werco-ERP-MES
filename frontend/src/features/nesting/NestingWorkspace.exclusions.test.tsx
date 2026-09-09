import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import NestingWorkspace from './NestingWorkspace';
import { NestingPortalContext } from './PortalContext';
import { createBlankQuote, type Quote } from './lib/quoting';
import { rect } from './lib/nesting';
import * as workerClient from './lib/nesting-worker-client';

const mockShowToast = jest.fn();
jest.mock('../../components/ui/Toast', () => ({ useToast: () => ({ showToast: mockShowToast }) }));
const fixture = (): Quote => ({
  ...createBlankQuote(),
  gap: 0,
  margin: 0,
  parts: [{ id: 'plate', name: 'Synthetic plate', loops: [rect(25.4, 25.4)], quantity: 1, rotate: true, color: 0 }],
  options: [{ id: 'test', width: 254, height: 254, enabled: true, price: 100 }],
});
function mount(quote: Quote) {
  return render(
    <NestingPortalContext.Provider value={document.body}>
      <NestingWorkspace initialQuote={quote} />
    </NestingPortalContext.Provider>
  );
}
const field = (name: string, value: string) => fireEvent.change(screen.getByLabelText(name), { target: { value } });
afterEach(() => {
  jest.restoreAllMocks();
  mockShowToast.mockClear();
});

test('editing exclusions invalidates the previous comparison and recomputes with exact stock areas', async () => {
  const worker = jest.spyOn(workerClient, 'compareSheetsInWorker');
  mount(fixture());
  fireEvent.click(screen.getByRole('button', { name: 'Compare sheets' }));
  await screen.findByRole('img', { name: /^Layout on / });
  expect(screen.getByRole('button', { name: 'Export review record' })).toBeEnabled();
  fireEvent.click(screen.getByRole('tab', { name: 'Stock sizes & prices' }));
  fireEvent.click(screen.getByRole('button', { name: /^Excluded areas for/ }));
  fireEvent.click(screen.getByRole('button', { name: 'New excluded area' }));
  field('Area label', 'Corner unavailable');
  field('Reason this material is unavailable', 'Estimator review');
  field('Size along X (in)', '2');
  field('Size along Y (in)', '2');
  fireEvent.click(screen.getByRole('button', { name: 'Add excluded area' }));
  await screen.findByRole('button', { name: 'Edit excluded area Corner unavailable' });
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  fireEvent.click(screen.getByRole('tab', { name: 'Quote workspace' }));
  expect(screen.queryByRole('img', { name: /^Layout on / })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Export review record' })).toBeDisabled();
  expect(worker).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole('button', { name: 'Compare sheets' }));
  await screen.findByRole('img', { name: /^Layout on / });
  expect(worker).toHaveBeenCalledTimes(2);
  expect(worker.mock.calls[1][0].options[0].exclusions?.[0].outline).toEqual(rect(50.8, 50.8));
  expect(
    screen.getByRole('img', { name: /^Layout on / }).querySelector('.stock-exclusion-overlay path')
  ).not.toBeNull();
  expect(screen.getByRole('button', { name: 'Export review record' })).toBeEnabled();
});

test('a rejected stock shrink retains both old dimensions and excluded geometry after blur', async () => {
  const quote = fixture();
  quote.options[0].exclusions = [
    {
      id: 'spot',
      label: 'Spot',
      reason: 'Unavailable material',
      clearance: 0,
      outline: { type: 'circle', cx: 228.6, cy: 228.6, r: 12.7 },
    },
  ];
  mount(quote);
  fireEvent.click(screen.getByRole('tab', { name: 'Stock sizes & prices' }));
  const length = screen.getByLabelText(/^Length/);
  fireEvent.focus(length);
  fireEvent.change(length, { target: { value: '5' } });
  await waitFor(() =>
    expect(mockShowToast).toHaveBeenCalledWith('error', expect.stringMatching(/inside the gross sheet/))
  );
  fireEvent.blur(length);
  expect(length).toHaveValue('10');
  fireEvent.click(screen.getByRole('button', { name: /^Excluded areas for/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Edit excluded area Spot' }));
  expect(screen.getByLabelText('Center X (in)')).toHaveValue('9');
  expect(screen.getByLabelText('Center Y (in)')).toHaveValue('9');
});
