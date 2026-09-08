import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import NestingWorkspace from './NestingWorkspace';
import { NestingPortalContext } from './PortalContext';
import { createBlankQuote, type Quote } from './lib/quoting';
import { rect } from './lib/nesting';
import { projectFromFile } from './lib/quote-project';
import * as workerClient from './lib/nesting-worker-client';

const mockShowToast = jest.fn();
jest.mock('../../components/ui/Toast', () => ({ useToast: () => ({ showToast: mockShowToast }) }));

function quoteFixture(): Quote {
  return {
    ...createBlankQuote(),
    gap: 0,
    margin: 0,
    parts: [{ id: 'plate', name: 'Synthetic plate', loops: [rect(40, 20)], quantity: 1, rotate: true, color: 0 }],
    options: [{ id: 'test-stock', width: 120, height: 80, enabled: true, price: 100 }],
  };
}
function mount(initialQuote?: Quote) {
  return render(
    <NestingPortalContext.Provider value={document.body}>
      <NestingWorkspace initialQuote={initialQuote} />
    </NestingPortalContext.Provider>
  );
}
function editOrientation() {
  fireEvent.click(screen.getByRole('button', { name: 'Edit rotation and grain for Synthetic plate' }));
}
async function compare() {
  fireEvent.click(screen.getByRole('button', { name: 'Compare sheets' }));
  await screen.findByRole('img', { name: /^Layout on / });
}
async function readBlob(blob: Blob) {
  let result = '';
  await act(async () => {
    result = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(reader.error);
      reader.readAsText(blob);
    });
  });
  return result;
}

describe('rotation and grain controls in the quote workspace', () => {
  beforeEach(() => mockShowToast.mockClear());
  afterEach(() => jest.restoreAllMocks());

  it('invalidates prior comparisons for each orientation edit and preserves restrictions across workspace tabs', async () => {
    const worker = jest.spyOn(workerClient, 'compareSheetsInWorker');
    mount(quoteFixture());
    expect(worker).not.toHaveBeenCalled();
    await compare();
    expect(screen.getByRole('button', { name: 'Export review record' })).toBeEnabled();
    editOrientation();
    expect(screen.getByLabelText('Allowed rotation for Synthetic plate')).toHaveValue('quarter-turn');
    fireEvent.change(screen.getByLabelText('Allowed rotation for Synthetic plate'), { target: { value: 'half-turn' } });
    expect(screen.queryByRole('img', { name: /^Layout on / })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Export review record' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Part grain for Synthetic plate'), { target: { value: 'x' } });
    expect(screen.getByText(/Part grain is required, but sheet grain is unknown/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Sheet grain'), { target: { value: 'x' } });
    expect(worker).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Permitted on this stock: 0° / 180°. Mirroring prohibited.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: 'Stock sizes & prices' }));
    fireEvent.click(screen.getByRole('tab', { name: 'Quote workspace' }));
    expect(screen.getByLabelText('Allowed rotation for Synthetic plate')).toHaveValue('half-turn');
    expect(screen.getByLabelText('Part grain for Synthetic plate')).toHaveValue('x');
    expect(screen.getByLabelText('Sheet grain')).toHaveValue('x');
    await compare();
    expect(worker).toHaveBeenCalledTimes(2);
    expect(worker.mock.calls[1][0]).toMatchObject({
      grainAxis: 'x',
      parts: [{ rotationMode: 'half-turn', grainAxis: 'x' }],
    });
    expect(screen.getByRole('button', { name: 'Export review record' })).toBeEnabled();
    fireEvent.change(screen.getByLabelText('Sheet grain'), { target: { value: 'y' } });
    expect(screen.queryByRole('img', { name: /^Layout on / })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Export review record' })).toBeDisabled();
    expect(screen.getByText(/Permitted rotations cannot align/)).toBeInTheDocument();
    expect(worker).toHaveBeenCalledTimes(2);
  });

  it('shows unresolved grain without a complete price, then saves restrictions and reopens them only by explicit Open', async () => {
    const quote = quoteFixture();
    quote.parts[0] = { ...quote.parts[0], rotationMode: 'half-turn', grainAxis: 'x' };
    const worker = jest.spyOn(workerClient, 'compareSheetsInWorker');
    const createURL = jest.spyOn(URL, 'createObjectURL');
    jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    const first = mount(quote);
    fireEvent.click(screen.getByRole('button', { name: 'Compare sheets' }));
    await screen.findByRole('heading', { name: 'Parts requiring review' });
    expect(screen.getByRole('heading', { name: 'No placements on this sheet' }).parentElement).toHaveTextContent(
      'Part grain is required, but sheet grain is unknown. Assign the sheet grain direction before nesting.'
    );
    expect(screen.queryByText(/1 \/ 1 parts covered/)).not.toBeInTheDocument();
    expect(screen.getByText('Not priced')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Sheet grain'), { target: { value: 'x' } });
    await compare();
    expect(worker).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    const blob = createURL.mock.calls[0][0];
    if (!(blob instanceof Blob)) throw new Error('Missing saved estimate blob.');
    const savedText = await readBlob(blob);
    const saved = JSON.parse(savedText);
    expect(saved.version).toBe(6);
    expect(projectFromFile(saved).groups[0].quote).toMatchObject({
      grainAxis: 'x',
      parts: [{ rotationMode: 'half-turn', grainAxis: 'x' }],
    });
    first.unmount();
    const second = mount();
    expect(screen.getByText('0 designs')).toBeInTheDocument();
    expect(screen.getByLabelText('Sheet grain')).toHaveValue('');
    expect(screen.queryByLabelText('Allowed rotation for Synthetic plate')).not.toBeInTheDocument();
    expect(screen.queryByRole('img', { name: /^Layout on / })).not.toBeInTheDocument();
    expect(worker).toHaveBeenCalledTimes(2);
    const input = second.container.querySelector<HTMLInputElement>('input[type="file"][accept=".json"]');
    if (!input) throw new Error('Missing estimate file input.');
    const file = new File([savedText], 'synthetic.estimate.json', { type: 'application/json' });
    Object.defineProperty(file, 'text', { value: async () => savedText });
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(screen.getByLabelText('Sheet grain')).toHaveValue('x'));
    editOrientation();
    expect(screen.getByLabelText('Allowed rotation for Synthetic plate')).toHaveValue('half-turn');
    expect(screen.getByLabelText('Part grain for Synthetic plate')).toHaveValue('x');
    expect(screen.queryByRole('img', { name: /^Layout on / })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Export review record' })).toBeDisabled();
    expect(worker).toHaveBeenCalledTimes(2);
  });
});
