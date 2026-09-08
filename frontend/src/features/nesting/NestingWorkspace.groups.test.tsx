import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import NestingWorkspace from './NestingWorkspace';
import { NestingPortalContext } from './PortalContext';
import { bounds, rect } from './lib/nesting';
import { createBlankQuote, type Quote } from './lib/quoting';
import { projectFromFile } from './lib/quote-project';
import * as workerClient from './lib/nesting-worker-client';

const mockShowToast = jest.fn();
jest.mock('../../components/ui/Toast', () => ({ useToast: () => ({ showToast: mockShowToast }) }));

function renderWorkspace(initialQuote?: Quote) {
  return render(
    <NestingPortalContext.Provider value={document.body}>
      <NestingWorkspace initialQuote={initialQuote} />
    </NestingPortalContext.Provider>
  );
}

function dxfFile(name: string, width?: number) {
  const content =
    width === undefined
      ? 'invalid drawing'
      : [
          0,
          'SECTION',
          2,
          'HEADER',
          9,
          '$INSUNITS',
          70,
          4,
          0,
          'ENDSEC',
          0,
          'SECTION',
          2,
          'ENTITIES',
          0,
          'LWPOLYLINE',
          90,
          4,
          70,
          1,
          10,
          0,
          20,
          0,
          10,
          width,
          20,
          0,
          10,
          width,
          20,
          50,
          10,
          0,
          20,
          50,
          0,
          'ENDSEC',
          0,
          'EOF',
          '',
        ].join('\n');
  const file = new File([content], name, { type: 'application/dxf' });
  const text = jest.fn().mockResolvedValue(content);
  Object.defineProperty(file, 'text', { value: text });
  return { file, text };
}
function fileInput(container: HTMLElement, accept: string) {
  const input = container.querySelector<HTMLInputElement>(`input[type="file"][accept="${accept}"]`);
  if (!input) throw new Error(`Missing ${accept} file input.`);
  return input;
}
async function readBlob(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}
function selectGroup(material: string) {
  const input = screen.getByRole('combobox', { name: 'Material & thickness group' });
  const option = within(input).getByRole('option', { name: new RegExp(`^${material}`) });
  if (!(option instanceof HTMLOptionElement)) throw new Error('Expected a native material group option.');
  fireEvent.change(input, { target: { value: option.value } });
}
function setPrice(value: string) {
  fireEvent.click(screen.getByRole('tab', { name: 'Stock sizes & prices' }));
  fireEvent.change(screen.getAllByLabelText(/^Price per sheet/)[0], { target: { value } });
  fireEvent.click(screen.getByRole('tab', { name: 'Quote workspace' }));
}

describe('material and thickness group workflow', () => {
  beforeEach(() => mockShowToast.mockClear());
  afterEach(() => jest.restoreAllMocks());

  it('can cancel assignment without reading a file or changing the empty estimate', async () => {
    const worker = jest.spyOn(workerClient, 'compareSheetsInWorker');
    const { container } = renderWorkspace();
    const file = dxfFile('unread.dxf', 100);
    fireEvent.change(fileInput(container, '.dxf'), { target: { files: [file.file] } });
    const dialog = await screen.findByRole('dialog', { name: 'Assign DXF materials and thicknesses' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(file.text).not.toHaveBeenCalled();
    expect(screen.getByText('0 designs')).toBeInTheDocument();
    expect(worker).not.toHaveBeenCalled();
  });

  it('keeps duplicate-named files in their assigned groups and saves quantities, prices, and manual spacing', async () => {
    const worker = jest.spyOn(workerClient, 'compareSheetsInWorker');
    const createURL = jest.spyOn(URL, 'createObjectURL');
    jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    const first = renderWorkspace();
    expect(worker).not.toHaveBeenCalled();
    const files = [dxfFile('same.dxf', 100), dxfFile('broken.dxf'), dxfFile('same.dxf', 200)];
    fireEvent.change(fileInput(first.container, '.dxf'), { target: { files: files.map(row => row.file) } });
    const assignment = await screen.findByRole('dialog', { name: 'Assign DXF materials and thicknesses' });
    expect(files.every(row => row.text.mock.calls.length === 0)).toBe(true);
    const assign = within(assignment);
    fireEvent.click(assign.getByRole('button', { name: 'Clear selection' }));
    fireEvent.click(assign.getByRole('checkbox', { name: 'Select file 3: same.dxf' }));
    fireEvent.change(assign.getByLabelText('Material for selected files'), { target: { value: 'Aluminum' } });
    fireEvent.change(assign.getByLabelText(/^Thickness for selected files/), { target: { value: '1/4' } });
    fireEvent.click(assign.getByRole('button', { name: 'Apply to 1 selected' }));
    fireEvent.click(assign.getByRole('button', { name: 'Import all 3 files' }));
    const result = await screen.findByRole('dialog', { name: 'DXF import results' });
    expect(within(result).getByRole('status')).toHaveTextContent('2 imported');
    expect(within(result).getByRole('status')).toHaveTextContent('1 skipped');
    expect(files.every(row => row.text.mock.calls.length === 1)).toBe(true);
    fireEvent.click(within(result).getByRole('button', { name: 'Review parts' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    fireEvent.change(screen.getByLabelText('Estimate name'), { target: { value: 'Mixed purchase' } });
    selectGroup('Carbon steel');
    fireEvent.change(screen.getByLabelText('Quantity for same'), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText(/^Part gap/), { target: { value: '0.3' } });
    fireEvent.change(screen.getByLabelText(/^Edge margin/), { target: { value: '0.8' } });
    fireEvent.change(screen.getByLabelText(/^Thickness/), { target: { value: '3/16' } });
    expect(screen.getByRole('switch', { name: 'Auto quoting allowance' })).not.toBeChecked();
    expect(screen.getByLabelText(/^Part gap/)).toHaveValue('0.3');
    expect(screen.getByLabelText(/^Edge margin/)).toHaveValue('0.8');
    setPrice('100');

    selectGroup('Aluminum');
    expect(screen.getByLabelText(/^Thickness/)).toHaveValue('0.25');
    expect(screen.getByLabelText(/^Part gap/)).toHaveValue('0.25');
    expect(screen.getByLabelText(/^Edge margin/)).toHaveValue('0.5');
    expect(screen.getByRole('switch', { name: 'Auto quoting allowance' })).toBeChecked();
    fireEvent.change(screen.getByLabelText('Quantity for same'), { target: { value: '3' } });
    setPrice('200');
    expect(worker).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Compare sheets' }));
    await screen.findByText(/3 \/ 3 parts covered/);
    expect(worker).toHaveBeenCalledTimes(2);
    const compared = worker.mock.calls.map(([quote]) => quote);
    expect(compared.map(quote => quote.material)).toEqual(['Carbon steel', 'Aluminum']);
    expect(compared.map(quote => quote.parts[0].quantity)).toEqual([2, 3]);
    expect(compared.map(quote => bounds(quote.parts[0].loops[0]).width)).toEqual([100, 200]);
    expect(compared.map(quote => quote.options[0].price)).toEqual([100, 200]);
    expect(compared[0].gap).toBeCloseTo(7.62);
    expect(compared[1].gap).toBeCloseTo(6.35);

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    const blob = createURL.mock.calls[0]?.[0];
    if (!(blob instanceof Blob)) throw new Error('Save did not create a project Blob.');
    const savedText = await readBlob(blob);
    const serialized = JSON.parse(savedText);
    expect(serialized.version).toBe(15);
    expect(serialized.units).toBe('in');
    const project = projectFromFile(serialized);
    expect(project.name).toBe('Mixed purchase');
    expect(project.groups).toHaveLength(2);
    expect(project.groups.map(group => group.quote.spacingMode)).toEqual(['manual', 'auto']);
    expect(project.groups.map(group => group.quote.parts[0].quantity)).toEqual([2, 3]);
    expect(project.groups.map(group => group.quote.options[0].price)).toEqual([100, 200]);
    expect(project.groups[0].quote.margin).toBeCloseTo(20.32);
    first.unmount();

    const reopened = renderWorkspace();
    const file = new File([savedText], 'mixed.estimate.json', { type: 'application/json' });
    Object.defineProperty(file, 'text', { value: jest.fn().mockResolvedValue(savedText) });
    fireEvent.change(fileInput(reopened.container, '.json'), { target: { files: [file] } });
    await waitFor(() => expect(screen.getByLabelText('Estimate name')).toHaveValue('Mixed purchase'));
    expect(screen.getByRole('combobox', { name: 'Material & thickness group' })).toHaveValue(project.activeGroupId);
    expect(screen.getByLabelText('Quantity for same')).toHaveValue(3);
    expect(screen.queryByRole('img', { name: /^Layout on / })).not.toBeInTheDocument();
    expect(worker).toHaveBeenCalledTimes(2);
    const beforeUnload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(beforeUnload);
    expect(beforeUnload.defaultPrevented).toBe(false);
    selectGroup('Carbon steel');
    expect(screen.getByLabelText('Quantity for same')).toHaveValue(2);
    expect(screen.getByLabelText(/^Part gap/)).toHaveValue('0.3');
    expect(screen.getByLabelText(/^Edge margin/)).toHaveValue('0.8');
    expect(screen.getByRole('switch', { name: 'Auto quoting allowance' })).not.toBeChecked();
    fireEvent.click(screen.getByRole('tab', { name: 'Stock sizes & prices' }));
    expect(screen.getAllByLabelText(/^Price per sheet/)[0]).toHaveValue(100);
  }, 15000);

  it('clears a cached layout when its last part is removed', async () => {
    const quote = createBlankQuote();
    quote.parts = [{ id: 'last', name: 'Last plate', loops: [rect(100, 50)], quantity: 1, color: 0, rotate: true }];
    const worker = jest.spyOn(workerClient, 'compareSheetsInWorker');
    renderWorkspace(quote);
    expect(worker).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Compare sheets' }));
    await screen.findByRole('img', { name: /^Layout on / });
    fireEvent.click(screen.getByRole('button', { name: 'Remove Last plate' }));
    expect(screen.getByText('0 designs')).toBeInTheDocument();
    expect(screen.queryByRole('img', { name: /^Layout on / })).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Add parts to start an estimate' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Compare sheets' })).toBeDisabled();
  });
});
