import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import NestingWorkspace from './NestingWorkspace';
import { NestingPortalContext } from './PortalContext';
import api from '../../services/api';
import { catalogQuoteFixture } from '../../test-utils/nestingCatalogFixtures';
import { projectFromFile } from './lib/quote-project';
import * as workerClient from './lib/nesting-worker-client';
import type { Quote } from './lib/quoting';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { getNestingMaterials: jest.fn(), resolveNestingMaterial: jest.fn() },
}));
const mockShowToast = jest.fn();
jest.mock('../../components/ui/Toast', () => ({ useToast: () => ({ showToast: mockShowToast }) }));
const getMaterials = jest.mocked(api.getNestingMaterials);
const resolveMaterial = jest.mocked(api.resolveNestingMaterial);

function renderWorkspace(quote: Quote) {
  return render(
    <NestingPortalContext.Provider value={document.body}>
      <NestingWorkspace initialQuote={quote} companyId={2} />
    </NestingPortalContext.Provider>
  );
}
async function blobText(blob: Blob) {
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
async function applyPrices() {
  fireEvent.click(screen.getByRole('button', { name: 'Resolve sheet prices' }));
  const checkbox = await screen.findByRole('checkbox', { name: /^I am using these values as USD/ });
  fireEvent.click(checkbox);
  fireEvent.click(screen.getByRole('button', { name: 'Apply reviewed USD prices' }));
  await waitFor(() => expect(screen.getAllByLabelText(/^Price per sheet/)[0]).toHaveValue(147.23));
}

describe('catalog source lifecycle in the estimate workspace', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    const fixture = catalogQuoteFixture();
    getMaterials.mockResolvedValue({ schema_version: 1, items: [fixture.catalog], total: 1, offset: 0, limit: 200 });
    resolveMaterial.mockResolvedValue(fixture.resolution);
  });
  afterEach(() => jest.restoreAllMocks());

  it('saves source evidence but requires new resolution and USD review after opening the saved file', async () => {
    const { quote, binding } = catalogQuoteFixture();
    quote.name = 'Reviewed catalog estimate';
    const worker = jest.spyOn(workerClient, 'compareSheetsInWorker');
    const createURL = jest.spyOn(URL, 'createObjectURL');
    jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    const { container } = renderWorkspace(quote);
    fireEvent.click(screen.getByRole('tab', { name: 'Stock sizes & prices' }));
    await screen.findByRole('button', { name: 'Refresh catalog' });
    expect(screen.getByLabelText('Catalog material')).toHaveValue('11');
    expect(screen.getAllByLabelText(/^Price per sheet/)[0]).toBeDisabled();
    await applyPrices();
    expect(screen.getByText(/USD is an estimator assumption/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: 'Quote workspace' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    const blob = createURL.mock.calls[0][0];
    if (!(blob instanceof Blob)) throw new Error('Save did not create a project blob.');
    const text = await blobText(blob);
    const saved = JSON.parse(text);
    expect(saved.version).toBe(5);
    const project = projectFromFile(saved);
    expect(project.groups[0].quote.materialBinding).toMatchObject({
      companyId: 2,
      catalog: binding.catalog,
      acknowledgement: { contentHash: binding.resolution!.content_hash, currency: 'USD', reviewed: true },
    });
    const file = new File([text], 'catalog.estimate.json', { type: 'application/json' });
    Object.defineProperty(file, 'text', { value: async () => text });
    const input = container.querySelector<HTMLInputElement>('input[type="file"][accept=".json"]');
    if (!input) throw new Error('Missing estimate file input.');
    fireEvent.change(input, { target: { files: [file] } });
    fireEvent.click(screen.getByRole('tab', { name: 'Stock sizes & prices' }));
    await waitFor(() => expect(screen.getAllByLabelText(/^Price per sheet/)[0]).toHaveValue(null));
    expect(screen.getByLabelText('Catalog material')).toHaveValue('11');
    expect(screen.getByText('Saved or stale source snapshot')).toBeInTheDocument();
    expect(screen.queryByRole('checkbox', { name: /^I am using these values as USD/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Apply reviewed USD prices' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: 'Quote workspace' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    const reopenedBlob = createURL.mock.calls[1][0];
    if (!(reopenedBlob instanceof Blob)) throw new Error('Second save did not create a project blob.');
    const reopened = projectFromFile(JSON.parse(await blobText(reopenedBlob))).groups[0].quote;
    expect(reopened.materialBinding?.resolution).toEqual(binding.resolution);
    expect(reopened.materialBinding?.acknowledgement).toBeUndefined();
    expect(reopened.options.every(option => option.price === null)).toBe(true);
    expect(worker).not.toHaveBeenCalled();
  });

  it('clears acknowledged prices when stock dimensions change and allows manual prices only after clearing the source', async () => {
    renderWorkspace(catalogQuoteFixture().quote);
    fireEvent.click(screen.getByRole('tab', { name: 'Stock sizes & prices' }));
    await applyPrices();
    fireEvent.change(screen.getAllByLabelText(/^Width/)[0], { target: { value: '49' } });
    expect(screen.getAllByLabelText(/^Price per sheet/).every(input => (input as HTMLInputElement).value === '')).toBe(
      true
    );
    expect(screen.queryByRole('button', { name: 'Apply reviewed USD prices' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Clear catalog source and prices' }));
    expect(screen.getByLabelText('Catalog material')).toHaveValue('');
    const price = screen.getAllByLabelText(/^Price per sheet/)[0];
    expect(price).toBeEnabled();
    fireEvent.change(price, { target: { value: '199.99' } });
    expect(price).toHaveValue(199.99);
  });

  it('clears applied pricing if a completed refreshed catalog no longer includes the selected record', async () => {
    renderWorkspace(catalogQuoteFixture().quote);
    fireEvent.click(screen.getByRole('tab', { name: 'Stock sizes & prices' }));
    await applyPrices();
    getMaterials.mockResolvedValueOnce({ schema_version: 1, items: [], total: 0, offset: 0, limit: 200 });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh catalog' }));
    await waitFor(() => expect(screen.getAllByLabelText(/^Price per sheet/)[0]).toHaveValue(null));
    expect(screen.queryByRole('button', { name: 'Apply reviewed USD prices' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Catalog material')).toHaveValue('11');
  });
});
