import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import NestingWorkspace from './NestingWorkspace';
import { NestingPortalContext } from './PortalContext';
import { compareSheets, createBlankQuote, type Quote } from './lib/quoting';
import { rect } from './lib/nesting';
import * as workerClient from './lib/nesting-worker-client';

const mockShowToast = jest.fn();
jest.mock('../../components/ui/Toast', () => ({ useToast: () => ({ showToast: mockShowToast }) }));
function quoteFixture(): Quote {
  return {
    ...createBlankQuote(),
    gap: 3.175,
    margin: 6.35,
    parts: [{ id: 'plate', name: 'Synthetic plate', loops: [rect(25.4, 25.4)], quantity: 1, rotate: false, color: 0 }],
    options: [{ id: 'synthetic-stock', width: 254, height: 127, enabled: true, price: 100 }],
  };
}
function mount() {
  return render(
    <NestingPortalContext.Provider value={document.body}>
      <NestingWorkspace initialQuote={quoteFixture()} />
    </NestingPortalContext.Provider>
  );
}
describe('potential leftover review in the estimate workspace', () => {
  const pointerEventDescriptor = Object.getOwnPropertyDescriptor(window, 'PointerEvent');
  beforeAll(() => {
    // Base UI forwards switch activation with PointerEvent; this jsdom version
    // lacks that browser constructor. MouseEvent supplies the click semantics.
    Object.defineProperty(window, 'PointerEvent', { configurable: true, value: MouseEvent });
  });
  afterAll(() => {
    if (pointerEventDescriptor) Object.defineProperty(window, 'PointerEvent', pointerEventDescriptor);
    else Reflect.deleteProperty(window, 'PointerEvent');
  });
  beforeEach(() => mockShowToast.mockClear());
  afterEach(() => jest.restoreAllMocks());

  it('shows review-only geometry with zero credit and hides stale regions immediately after quantity changes', async () => {
    const worker = jest.spyOn(workerClient, 'compareSheetsInWorker');
    mount();
    expect(screen.queryByRole('region', { name: 'Leftover material review' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Compare sheets' }));
    let panel = within(await screen.findByRole('region', { name: 'Leftover material review' }));
    expect(panel.getByRole('heading', { name: 'Review potential leftovers · sheet 1' })).toBeInTheDocument();
    expect(panel.getByText('$0 credited')).toBeInTheDocument();
    expect(panel.getByText(/A connected region can still be an unusable skeleton/)).toBeInTheDocument();
    expect(panel.getByText('Extents do not guarantee a usable rectangle.')).toBeInTheDocument();
    const highlight = panel.getByRole('button', { name: 'Highlight leftover region 1' });
    expect(highlight).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(highlight);
    expect(highlight).toHaveAttribute('aria-pressed', 'true');
    const layout = screen.getByRole('img', { name: /^Layout on / });
    expect(within(layout).getByText(/^Potential leftover 1:/)).toHaveTextContent('Requires review; no value credited.');
    const toggle = screen.getByRole('switch', { name: /Show potential leftovers/ });
    expect(toggle).toBeChecked();
    fireEvent.click(toggle);
    expect(within(layout).queryByText(/^Potential leftover 1:/)).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Leftover material review' })).toBeInTheDocument();
    fireEvent.click(toggle);
    expect(worker).toHaveBeenCalledTimes(1);
    fireEvent.change(screen.getByLabelText('Quantity for Synthetic plate'), { target: { value: '2' } });
    expect(screen.queryByRole('region', { name: 'Leftover material review' })).not.toBeInTheDocument();
    expect(screen.queryByRole('img', { name: /^Layout on / })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Export review record' })).toBeDisabled();
    expect(worker).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Compare sheets' }));
    panel = within(await screen.findByRole('region', { name: 'Leftover material review' }));
    expect(panel.getByRole('button', { name: 'Highlight leftover region 1' })).toHaveAttribute('aria-pressed', 'false');
    expect(panel.getByText('$0 credited')).toBeInTheDocument();
    expect(worker).toHaveBeenCalledTimes(2);
  });

  it('retains a complete material estimate when leftover analysis fails without claiming remaining area or value', async () => {
    const comparison = compareSheets(quoteFixture());
    comparison.results = comparison.results.map(result => ({
      ...result,
      leftovers: undefined,
      leftoverError: 'Synthetic analysis budget exceeded.',
    }));
    jest.spyOn(workerClient, 'compareSheetsInWorker').mockResolvedValueOnce(comparison);
    mount();
    fireEvent.click(screen.getByRole('button', { name: 'Compare sheets' }));
    const panel = within(await screen.findByRole('region', { name: 'Leftover material review' }));
    expect(panel.getByRole('heading', { name: 'Leftover analysis unavailable' })).toBeInTheDocument();
    expect(panel.getByText(/No remnant area or value is claimed/)).toHaveTextContent(
      'Synthetic analysis budget exceeded.'
    );
    expect(panel.queryByRole('button', { name: /^Highlight leftover region/ })).not.toBeInTheDocument();
    expect(screen.getByText(/1 \/ 1 parts covered/)).toBeInTheDocument();
    expect(screen.getByRole('img', { name: /^Layout on / })).toBeInTheDocument();
    expect(panel.getByText('$0 credited')).toBeInTheDocument();
    expect(comparison.results[0].cost).toBe(100);
  });
});
