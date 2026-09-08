import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import MaterialNesting from './MaterialNesting';
import { bounds } from '../features/nesting/lib/nesting';
import { compareSheets } from '../features/nesting/lib/quoting';
import { projectFromFile } from '../features/nesting/lib/quote-project';

// Vite supplies this CSS as text. Keep the real shadow host, workspace and
// Base UI portals; only the build-time CSS transform and ERP toast are stubbed.
jest.mock('../features/nesting/nesting.css?inline', () => '', { virtual: true });
const mockShowToast = jest.fn();
let mockUser = { id: 7, company_id: 10 };
let mockCurrentCompany = { id: 10 };
jest.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: mockUser }) }));
jest.mock('../context/CompanyContext', () => ({ useCompany: () => ({ currentCompany: mockCurrentCompany }) }));
jest.mock('../components/ui/Toast', () => ({
  useToast: () => ({ showToast: mockShowToast }),
}));

// ASCII DXF: a 10 x 5 inch plate with a 1 inch diameter hole. Explicit
// INSUNITS makes the saved geometry's scale independently checkable.
const plateDXF = [
  '0',
  'SECTION',
  '2',
  'HEADER',
  '9',
  '$INSUNITS',
  '70',
  '1',
  '0',
  'ENDSEC',
  '0',
  'SECTION',
  '2',
  'ENTITIES',
  '0',
  'LWPOLYLINE',
  '90',
  '4',
  '70',
  '1',
  '10',
  '0',
  '20',
  '0',
  '10',
  '10',
  '20',
  '0',
  '10',
  '10',
  '20',
  '5',
  '10',
  '0',
  '20',
  '5',
  '0',
  'CIRCLE',
  '10',
  '5',
  '20',
  '2.5',
  '40',
  '0.5',
  '0',
  'ENDSEC',
  '0',
  'EOF',
  '',
].join('\n');

function dxfFile(name: string) {
  const file = new File([plateDXF], name, { type: 'application/dxf' });
  // jsdom File lacks the browser's Blob.text API.
  const text = jest.fn().mockResolvedValue(plateDXF);
  Object.defineProperty(file, 'text', { value: text });
  return { file, text };
}

function mountWorkspace() {
  const rendered = render(<MaterialNesting />);
  const shadow = screen.getByTestId('material-nesting-host').shadowRoot;
  if (!shadow) throw new Error('Material Nesting did not attach its shadow root.');
  const mount = shadow.querySelector<HTMLElement>('[data-nesting-mount]');
  if (!mount) throw new Error('Material Nesting did not create its portal target.');
  return { ...rendered, shadow, mount, ui: within(mount) };
}

function fileInput(mount: HTMLElement, accept: string) {
  const input = mount.querySelector<HTMLInputElement>(`input[type="file"][accept="${accept}"]`);
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

function beforeUnloadIsPrevented() {
  const event = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
}

function expectBlankWorkspace(ui: ReturnType<typeof within>) {
  expect(ui.getByLabelText('Estimate name')).toHaveValue('New material estimate');
  expect(ui.getByRole('combobox', { name: 'Material' })).toHaveTextContent('Carbon steel');
  expect(ui.getByText('0 designs')).toBeInTheDocument();
  expect(ui.queryByLabelText(/^Quantity for /)).not.toBeInTheDocument();
  expect(ui.getByRole('heading', { name: 'Add parts to start an estimate' })).toBeInTheDocument();
  expect(ui.getByRole('heading', { name: 'Your nest preview' })).toBeInTheDocument();
  expect(ui.queryByText(/parts covered/)).not.toBeInTheDocument();
  expect(ui.queryByRole('img', { name: /^Layout on / })).not.toBeInTheDocument();
  expect(ui.getByRole('button', { name: 'Compare sheets' })).toBeDisabled();
}

async function editEstimate(ui: ReturnType<typeof within>) {
  fireEvent.change(ui.getByLabelText('Estimate name'), { target: { value: 'Customer bracket run' } });
  fireEvent.click(ui.getByRole('combobox', { name: 'Material' }));
  await userEvent.setup().click(await ui.findByRole('option', { name: 'Aluminum' }));
  fireEvent.click(ui.getByRole('button', { name: 'Add basic shape' }));
  const dialog = await ui.findByRole('dialog', { name: 'Add a part by size' });
  fireEvent.change(within(dialog).getByLabelText('Part name'), { target: { value: 'Customer plate' } });
  fireEvent.change(within(dialog).getByLabelText('Quantity'), { target: { value: '3' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add part' }));
  await waitFor(() => expect(ui.queryByRole('dialog')).not.toBeInTheDocument());
}

function expectEditedEstimate(ui: ReturnType<typeof within>) {
  expect(ui.getByLabelText('Estimate name')).toHaveValue('Customer bracket run');
  expect(ui.getByRole('combobox', { name: 'Material' })).toHaveTextContent('Aluminum');
  expect(ui.getByLabelText('Quantity for Customer plate')).toHaveValue(3);
  expect(ui.getByText('1 designs')).toBeInTheDocument();
}

describe('Material Nesting inside the ERP shadow host', () => {
  beforeEach(() => {
    mockShowToast.mockClear();
    mockUser = { id: 7, company_id: 10 };
    mockCurrentCompany = { id: 10 };
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('imports 100 actual files, reports every file inside the shadow root, and saves all quantities and geometry', async () => {
    const { shadow, mount, ui } = mountWorkspace();
    const files = Array.from({ length: 100 }, (_, index) => dxfFile(`Plate-${index + 1}.dxf`));
    const input = fileInput(mount, '.dxf');
    expect(input.multiple).toBe(true);
    fireEvent.change(input, { target: { files: files.map(({ file }) => file) } });

    const assignment = await ui.findByRole('dialog', { name: 'Assign DXF materials and thicknesses' });
    expect(assignment.getRootNode()).toBe(shadow);
    expect(files.every(({ text }) => text.mock.calls.length === 0)).toBe(true);
    fireEvent.click(within(assignment).getByRole('button', { name: 'Import all 100 files' }));
    const dialog = await ui.findByRole('dialog', { name: 'Importing DXF files' });
    expect(dialog.getRootNode()).toBe(shadow);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(ui.getByRole('button', { name: 'Compare sheets' })).toBeDisabled();
    // Each file yields and updates progress. Repeating a failing role query
    // formats the entire workspace DOM on every update, delaying those yields
    // on coverage-instrumented CI. Wait on the small existing dialog instead,
    // then check its accessible role and name once the import has completed.
    await waitFor(
      () => expect(dialog.querySelector('[data-slot="dialog-title"]')?.textContent).toBe('DXF import results'),
      { container: dialog, timeout: 30000 }
    );
    expect(ui.getByRole('dialog', { name: 'DXF import results' })).toBe(dialog);
    expect(within(dialog).getByRole('status')).toHaveTextContent('100 imported');
    expect(within(dialog).getByRole('status')).toHaveTextContent('0 skipped');
    expect(within(dialog).getByRole('status')).toHaveTextContent('100 designs added');
    expect(within(dialog).getByText('Plate-1.dxf')).toBeInTheDocument();
    expect(within(dialog).getByText('Plate-100.dxf')).toBeInTheDocument();
    expect(files.every(({ text }) => text.mock.calls.length === 1)).toBe(true);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Review parts' }));
    await waitFor(() => expect(ui.queryByRole('dialog')).not.toBeInTheDocument());
    expect(ui.getByText('100 designs')).toBeInTheDocument();
    expect(ui.getByRole('button', { name: 'Compare sheets' })).toBeEnabled();
    fireEvent.click(ui.getByRole('button', { name: 'Compare sheets' }));
    await ui.findByText(/100 \/ 100 parts covered/);

    const createURL = jest.spyOn(URL, 'createObjectURL');
    jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    fireEvent.click(ui.getByRole('button', { name: 'Save' }));
    const blob = createURL.mock.calls[0]?.[0];
    if (!(blob instanceof Blob)) throw new Error('Save did not create an estimate Blob.');
    const project = projectFromFile(JSON.parse(await readBlob(blob)));
    expect(project.groups).toHaveLength(1);
    const saved = project.groups[0].quote;
    expect(saved.parts).toHaveLength(100);
    expect(new Set(saved.parts.map(part => part.id)).size).toBe(100);
    expect(saved.parts.reduce((total, part) => total + part.quantity, 0)).toBe(100);
    const imported = saved.parts;
    expect(imported.every(part => part.quantity === 1 && part.loops.length === 2)).toBe(true);
    for (const part of imported) {
      expect(bounds(part.loops[0]).width).toBeCloseTo(254);
      expect(bounds(part.loops[0]).height).toBeCloseTo(127);
    }
    const comparison = compareSheets(saved);
    expect(comparison.recommendedId).toBeTruthy();
    expect(comparison.results.every(result => result.complete && result.nest?.placements.length === 100)).toBe(true);
    expect(mockShowToast).toHaveBeenCalledWith('success', '100 files imported · 100 designs added.');
  }, 60000);

  it('rejects a 101-file selection before reading or changing the estimate', async () => {
    const { mount, ui } = mountWorkspace();
    const files = Array.from({ length: 101 }, (_, index) => dxfFile(`Plate-${index}.dxf`));
    fireEvent.change(fileInput(mount, '.dxf'), { target: { files: files.map(({ file }) => file) } });
    expect(mockShowToast).toHaveBeenCalledWith('error', expect.stringContaining('up to 100 DXF files'));
    expect(files.every(({ text }) => text.mock.calls.length === 0)).toBe(true);
    expect(ui.getByText('0 designs')).toBeInTheDocument();
    expect(ui.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('keeps a select popup inside its dialog and adds a fractional-inch circle with the chosen quantity', async () => {
    const user = userEvent.setup();
    const { shadow, ui } = mountWorkspace();
    fireEvent.click(ui.getByRole('button', { name: 'Add basic shape' }));
    const dialog = await ui.findByRole('dialog', { name: 'Add a part by size' });
    expect(dialog.getRootNode()).toBe(shadow);
    fireEvent.change(within(dialog).getByLabelText('Part name'), { target: { value: 'Round spacer' } });
    fireEvent.click(within(dialog).getByRole('combobox', { name: 'Shape' }));
    const circle = await ui.findByRole('option', { name: 'Circle' });
    expect(circle.getRootNode()).toBe(shadow);
    expect(screen.queryByRole('option')).not.toBeInTheDocument();
    await user.click(circle);
    fireEvent.change(within(dialog).getByLabelText(/Diameter/), { target: { value: '2 1/2' } });
    fireEvent.change(within(dialog).getByLabelText('Quantity'), { target: { value: '3' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add part' }));
    await waitFor(() => expect(ui.queryByRole('dialog')).not.toBeInTheDocument());
    expect(ui.getByLabelText('Quantity for Round spacer')).toHaveValue(3);
    expect(ui.getByText('1 designs')).toBeInTheDocument();
    expect(mockShowToast).toHaveBeenCalledWith('success', 'Part added. Compare sheets to update the estimate.');
    const createURL = jest.spyOn(URL, 'createObjectURL');
    jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    fireEvent.click(ui.getByRole('button', { name: 'Save' }));
    const blob = createURL.mock.calls[0]?.[0];
    if (!(blob instanceof Blob)) throw new Error('Save did not create an estimate Blob.');
    const project = projectFromFile(JSON.parse(await readBlob(blob)));
    expect(project.groups).toHaveLength(1);
    const saved = project.groups[0].quote;
    expect(saved.parts.find(part => part.name === 'Round spacer')).toMatchObject({
      quantity: 3,
      loops: [{ type: 'circle', r: 31.75 }],
    });
  });

  it('keeps edits across workspace tabs but starts clean after leaving and reopening the route', async () => {
    const first = mountWorkspace();
    expectBlankWorkspace(first.ui);
    expect(beforeUnloadIsPrevented()).toBe(false);
    await editEstimate(first.ui);
    expect(beforeUnloadIsPrevented()).toBe(true);

    fireEvent.click(first.ui.getByRole('tab', { name: 'Stock sizes & prices' }));
    expect(first.ui.getByRole('tab', { name: 'Stock sizes & prices' })).toHaveAttribute('aria-selected', 'true');
    fireEvent.click(first.ui.getByRole('tab', { name: 'How estimates work' }));
    fireEvent.click(first.ui.getByRole('tab', { name: 'Quote workspace' }));
    expectEditedEstimate(first.ui);
    fireEvent.click(first.ui.getByRole('button', { name: 'Compare sheets' }));
    await first.ui.findByText(/3 \/ 3 parts covered/);
    first.unmount();

    // React Router unmounts the page on departure and creates a new one on entry.
    const second = mountWorkspace();
    expectBlankWorkspace(second.ui);
    expect(beforeUnloadIsPrevented()).toBe(false);
  });

  it('reopens an explicitly saved estimate after a fresh route entry', async () => {
    const createURL = jest.spyOn(URL, 'createObjectURL');
    jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    const first = mountWorkspace();
    await editEstimate(first.ui);
    fireEvent.click(first.ui.getByRole('button', { name: 'Save' }));
    const blob = createURL.mock.calls[0]?.[0];
    if (!(blob instanceof Blob)) throw new Error('Save did not create an estimate Blob.');
    const savedText = await readBlob(blob);
    expect(beforeUnloadIsPrevented()).toBe(false);
    first.unmount();

    const second = mountWorkspace();
    expectBlankWorkspace(second.ui);
    const input = fileInput(second.mount, '.json');
    const openPicker = jest.spyOn(input, 'click');
    fireEvent.click(second.ui.getByRole('button', { name: 'Open' }));
    expect(openPicker).toHaveBeenCalledTimes(1);
    const file = new File([savedText], 'customer-run.estimate.json', { type: 'application/json' });
    Object.defineProperty(file, 'text', { value: jest.fn().mockResolvedValue(savedText) });
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(second.ui.getByLabelText('Estimate name')).toHaveValue('Customer bracket run'));
    expectEditedEstimate(second.ui);
    expect(beforeUnloadIsPrevented()).toBe(false);
    fireEvent.click(second.ui.getByRole('button', { name: 'Compare sheets' }));
    await second.ui.findByText(/3 \/ 3 parts covered/);
    expect(mockShowToast).toHaveBeenCalledWith('success', 'Estimate loaded. Compare sheets to calculate requirements.');
  });

  it.each(['company', 'user'])(
    'starts empty when the active %s changes, including when switching back',
    async identity => {
      const first = mountWorkspace();
      await editEstimate(first.ui);
      if (identity === 'company') mockCurrentCompany = { id: 20 };
      else mockUser = { id: 8, company_id: 10 };
      first.rerender(<MaterialNesting />);
      expectBlankWorkspace(first.ui);
      expect(beforeUnloadIsPrevented()).toBe(false);
      mockCurrentCompany = { id: 10 };
      mockUser = { id: 7, company_id: 10 };
      first.rerender(<MaterialNesting />);
      expectBlankWorkspace(first.ui);
    }
  );
});
