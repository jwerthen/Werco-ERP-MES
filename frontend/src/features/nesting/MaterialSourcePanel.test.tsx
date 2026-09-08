import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import MaterialSourcePanel from './MaterialSourcePanel';
import api from '../../services/api';
import { catalogQuoteFixture } from '../../test-utils/nestingCatalogFixtures';
import type { NestingMaterialResolution } from '../../types/quoteNesting';
import { createBlankQuote } from './lib/quoting';

jest.mock('../../services/api', () => ({ __esModule: true, default: { resolveNestingMaterial: jest.fn() } }));
const resolveMaterial = jest.mocked(api.resolveNestingMaterial);

function props() {
  const fixture = catalogQuoteFixture();
  return {
    quote: fixture.quote,
    companyId: 2,
    catalog: {
      items: [fixture.catalog],
      total: 1,
      loading: false,
      error: '',
      complete: true,
      refresh: jest.fn(),
      loadMore: jest.fn(),
    },
    verifiedHashes: new Set([fixture.resolution.content_hash]),
    onResolved: jest.fn(),
    onBindingChange: jest.fn(),
    onApply: jest.fn(),
  };
}

describe('catalog pricing review gate', () => {
  beforeEach(() => jest.clearAllMocks());

  it('requires explicit catalog and basis selections without resolving or applying prices automatically', () => {
    const value = props();
    const view = render(<MaterialSourcePanel {...value} quote={createBlankQuote()} />);
    expect(screen.getByLabelText('Catalog material')).toHaveValue('');
    expect(screen.queryByLabelText('Price basis')).not.toBeInTheDocument();
    expect(resolveMaterial).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText('Catalog material'), { target: { value: '11' } });
    expect(value.onBindingChange).toHaveBeenLastCalledWith({ companyId: 2, catalog: value.catalog.items[0] });
    view.rerender(
      <MaterialSourcePanel
        {...value}
        quote={{ ...value.quote, materialBinding: value.onBindingChange.mock.calls[0][0] }}
      />
    );
    expect(screen.getByLabelText('Price basis')).toHaveValue('');
    expect(screen.getByRole('button', { name: 'Resolve sheet prices' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Price basis'), { target: { value: '0' } });
    expect(value.onBindingChange).toHaveBeenLastCalledWith({
      companyId: 2,
      catalog: value.catalog.items[0],
      priceBasis: 'per_lb',
      priceKey: null,
    });
    expect(value.onApply).not.toHaveBeenCalled();
    expect(resolveMaterial).not.toHaveBeenCalled();
  });

  it('requires a current session resolution and explicit USD review before applying numeric prices', async () => {
    const value = props();
    const resolution = value.quote.materialBinding!.resolution!;
    const view = render(<MaterialSourcePanel {...value} verifiedHashes={new Set()} />);
    expect(screen.getByText('Saved or stale source snapshot')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Apply reviewed USD prices' })).not.toBeInTheDocument();
    resolveMaterial.mockResolvedValueOnce(resolution);
    fireEvent.click(screen.getByRole('button', { name: 'Resolve sheet prices' }));
    await screen.findByRole('button', { name: 'Resolve sheet prices' });
    expect(resolveMaterial).toHaveBeenCalledWith(
      {
        catalog_material_id: 11,
        thickness_in: '0.125',
        price_basis: 'per_lb',
        expected_catalog_hash: value.catalog.items[0].catalog_hash,
        stock_options: [
          { id: '48x96', width_in: '48', length_in: '96' },
          { id: '48x120', width_in: '48', length_in: '120' },
          { id: '60x120', width_in: '60', length_in: '120' },
          { id: '60x144', width_in: '60', length_in: '144' },
          { id: '72x144', width_in: '72', length_in: '144' },
          { id: '84x144', width_in: '84', length_in: '144' },
        ],
      },
      expect.any(AbortSignal)
    );
    expect(value.onResolved).toHaveBeenCalledWith(expect.objectContaining({ resolution, acknowledgement: undefined }));
    view.rerender(<MaterialSourcePanel {...value} />);
    fireEvent.click(screen.getByRole('button', { name: 'Apply reviewed USD prices' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Acknowledge the currency assumption');
    expect(value.onApply).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('checkbox', { name: /^I am using these values as USD/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Apply reviewed USD prices' }));
    await act(async () => undefined);
    expect(value.onApply).toHaveBeenCalledWith(
      expect.objectContaining({
        acknowledgement: { contentHash: resolution.content_hash, currency: 'USD', reviewed: true },
      }),
      new Map(resolution.stocks.map(stock => [stock.id, Number(stock.sheet_cost)]))
    );
    expect(screen.getByText('Not recorded in catalog')).toBeInTheDocument();
    expect(screen.getByText(/not approved material data/)).toBeInTheDocument();
  });

  it.each(['thickness', 'company'] as const)('ignores an in-flight response after %s changes', async change => {
    const value = props();
    let finish!: (result: NestingMaterialResolution) => void;
    resolveMaterial.mockReturnValueOnce(
      new Promise(resolve => {
        finish = resolve;
      })
    );
    const view = render(<MaterialSourcePanel {...value} />);
    fireEvent.click(screen.getByRole('button', { name: 'Resolve sheet prices' }));
    const signal = resolveMaterial.mock.calls[0][1];
    view.rerender(
      <MaterialSourcePanel
        {...value}
        companyId={change === 'company' ? 3 : 2}
        quote={change === 'thickness' ? { ...value.quote, thickness: value.quote.thickness * 2 } : value.quote}
      />
    );
    expect(signal?.aborted).toBe(true);
    await act(async () => finish(value.quote.materialBinding!.resolution!));
    expect(value.onResolved).not.toHaveBeenCalled();
    expect(value.onApply).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Apply reviewed USD prices' })).not.toBeInTheDocument();
  });

  it('invalidates changed catalog snapshots and clears all prior review when selecting the refreshed record', () => {
    const value = props();
    const refreshed = { ...value.catalog.items[0], catalog_hash: 'c'.repeat(64) };
    render(<MaterialSourcePanel {...value} catalog={{ ...value.catalog, items: [refreshed] }} />);
    expect(screen.getByRole('alert')).toHaveTextContent('saved prices are stale');
    expect(screen.queryByRole('button', { name: 'Apply reviewed USD prices' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Use refreshed catalog record' }));
    expect(value.onBindingChange).toHaveBeenLastCalledWith({ companyId: 2, catalog: refreshed });
    fireEvent.click(screen.getByRole('button', { name: 'Clear catalog source and prices' }));
    expect(value.onBindingChange.mock.calls[value.onBindingChange.mock.calls.length - 1][0]).toBeUndefined();
  });

  it('reports permission failures without fabricating a zero price or a successful resolution', async () => {
    const value = props();
    resolveMaterial.mockRejectedValueOnce({ response: { data: { detail: 'Purchasing permission was revoked.' } } });
    render(<MaterialSourcePanel {...value} verifiedHashes={new Set()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Resolve sheet prices' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Purchasing permission was revoked.');
    expect(value.onResolved).not.toHaveBeenCalled();
    expect(value.onApply).not.toHaveBeenCalled();
  });

  it('treats a missing row as unavailable only after the full active catalog is known', () => {
    const value = props();
    const view = render(
      <MaterialSourcePanel {...value} catalog={{ ...value.catalog, items: [], total: 200, complete: false }} />
    );
    expect(screen.getByRole('button', { name: 'Apply reviewed USD prices' })).toBeInTheDocument();
    view.rerender(
      <MaterialSourcePanel {...value} catalog={{ ...value.catalog, items: [], total: 0, complete: true }} />
    );
    expect(screen.getByRole('alert')).toHaveTextContent('absent from the complete active catalog');
    expect(screen.queryByRole('button', { name: 'Apply reviewed USD prices' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Resolve sheet prices' })).toBeDisabled();
  });
});
