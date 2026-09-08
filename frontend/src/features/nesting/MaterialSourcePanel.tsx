import React, { useEffect, useId, useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import api from '../../services/api';
import type { Quote } from './lib/quoting';
import { formatCentralDateTime } from '../../utils/centralTime';
import {
  catalogFamily,
  decimalInches,
  hasAcknowledgedCatalogPricing,
  resolutionMatchesInputs,
  validateMaterialBinding,
  type MaterialBinding,
} from './lib/material-binding';
import { nestingApiMessage, type NestingCatalogState } from './useNestingCatalog';

const acknowledgementSchema = z.object({
  reviewed: z.boolean().refine(value => value, 'Acknowledge the currency assumption and unresolved metadata first.'),
});
const basisLabel = { per_lb: 'Per pound', per_cubic_inch: 'Per cubic inch', per_square_foot: 'Per square foot' };

export default function MaterialSourcePanel({
  quote,
  companyId,
  catalog,
  verifiedHashes,
  onBindingChange,
  onResolved,
  onApply,
}: {
  quote: Quote;
  companyId?: number;
  catalog: NestingCatalogState;
  verifiedHashes: ReadonlySet<string>;
  onResolved: (binding: MaterialBinding) => void;
  onBindingChange: (binding?: MaterialBinding) => void;
  onApply: (binding: MaterialBinding, prices: Map<string, number>) => void;
}) {
  const id = useId();
  const [search, setSearch] = useState('');
  const [resolving, setResolving] = useState(false);
  const [error, setError] = useState('');
  const request = useRef<AbortController | null>(null);
  const binding = quote.materialBinding;
  const resolved = binding?.resolution;
  const latestCatalog = catalog.items.find(item => item.id === binding?.catalog.id);
  const unavailableCatalog = !!binding && catalog.complete && !latestCatalog;
  const changedCatalog = !!binding && !!latestCatalog && latestCatalog.catalog_hash !== binding.catalog.catalog_hash;
  const current =
    !!resolved &&
    verifiedHashes.has(resolved.content_hash) &&
    !changedCatalog &&
    !unavailableCatalog &&
    resolutionMatchesInputs(quote) &&
    binding?.companyId === companyId;
  const applied = current && hasAcknowledgedCatalogPricing(quote);
  const form = useForm<{ reviewed: boolean }>({
    resolver: zodResolver(acknowledgementSchema),
    defaultValues: { reviewed: false },
  });
  const inputKey = JSON.stringify([
    companyId,
    binding?.catalog.id,
    binding?.catalog.catalog_hash,
    binding?.priceBasis,
    binding?.priceKey,
    quote.thickness,
    quote.options.map(option => [option.id, option.width, option.height]),
  ]);
  const latestKey = useRef(inputKey);
  latestKey.current = inputKey;
  useEffect(() => {
    setError('');
    setResolving(false);
    return () => request.current?.abort();
  }, [inputKey]);
  useEffect(() => form.reset({ reviewed: false }), [resolved?.content_hash, inputKey, form]);

  async function resolve() {
    if (!binding?.priceBasis || !companyId) return;
    const controller = new AbortController();
    request.current?.abort();
    request.current = controller;
    const key = inputKey;
    setResolving(true);
    setError('');
    try {
      const result = await api.resolveNestingMaterial(
        {
          catalog_material_id: binding.catalog.id,
          thickness_in: decimalInches(quote.thickness),
          stock_options: quote.options.map(option => ({
            id: option.id,
            width_in: decimalInches(option.height),
            length_in: decimalInches(option.width),
          })),
          price_basis: binding.priceBasis,
          ...(binding.priceKey !== null && binding.priceKey !== undefined ? { price_key: binding.priceKey } : {}),
          expected_catalog_hash: binding.catalog.catalog_hash,
        },
        controller.signal
      );
      if (controller.signal.aborted || key !== latestKey.current) return;
      if (result.company_id !== companyId) throw new Error('Material resolution belongs to another company.');
      const next = { ...binding, catalog: result.catalog_material, resolution: result, acknowledgement: undefined };
      validateMaterialBinding(next);
      onResolved(next);
    } catch (cause) {
      if (!controller.signal.aborted && key === latestKey.current) setError(nestingApiMessage(cause));
    } finally {
      if (!controller.signal.aborted && key === latestKey.current) setResolving(false);
    }
  }

  const items = catalog.items.filter(
    item => catalogFamily(item.category) && (!search || item.name.toLowerCase().includes(search.toLowerCase()))
  );
  const selectedPrice =
    binding?.priceBasis === undefined
      ? ''
      : String(
          binding.catalog.price_options.findIndex(
            option => option.price_basis === binding.priceBasis && option.price_key === (binding.priceKey ?? null)
          )
        );
  return (
    <section className="material-source-panel" aria-label="ERP material and pricing source">
      <div className="material-source-heading">
        <h3>ERP material & pricing</h3>
        <span className="source-review-badge">{applied ? 'Estimator-reviewed USD' : 'Review required'}</span>
      </div>
      <p className="helper inset-free">
        Choose the exact catalog record and price basis. Catalog metadata is incomplete; these prices are draft
        estimating inputs.
      </p>
      <label className="field-label" htmlFor={id + '-search'}>
        <span>Find catalog material</span>
        <input
          id={id + '-search'}
          className="assignment-input"
          type="search"
          value={search}
          onChange={event => setSearch(event.target.value)}
          placeholder="Name or grade"
          disabled={!companyId}
        />
      </label>
      <label className="field-label" htmlFor={id + '-material'}>
        <span>Catalog material</span>
        <select
          id={id + '-material'}
          className="assignment-select"
          value={binding?.catalog.id ?? ''}
          disabled={!companyId || catalog.loading}
          onChange={event => {
            const selected = catalog.items.find(item => item.id === Number(event.target.value));
            if (selected && companyId) onBindingChange({ companyId, catalog: selected });
            else onBindingChange();
          }}
        >
          <option value="">Select a catalog record explicitly</option>
          {binding && !items.some(item => item.id === binding.catalog.id) && (
            <option value={binding.catalog.id}>{binding.catalog.name} · saved selection</option>
          )}
          {items.map(item => (
            <option key={item.id} value={item.id}>
              {item.name} · ID {item.id}
            </option>
          ))}
        </select>
      </label>
      <div className="source-catalog-actions">
        <button
          type="button"
          className="text-button"
          disabled={!companyId || catalog.loading}
          onClick={catalog.refresh}
        >
          {catalog.loading ? 'Reading catalog…' : 'Refresh catalog'}
        </button>
        {catalog.items.length < catalog.total && (
          <button type="button" className="text-button" disabled={catalog.loading} onClick={catalog.loadMore}>
            Load more materials
          </button>
        )}
      </div>
      {!companyId && <p className="helper inset-free">An active ERP company is required to read catalog sources.</p>}
      {catalog.error && (
        <p className="inline-error" role="alert">
          {catalog.error} Geometry nesting remains available.
        </p>
      )}
      {binding && (
        <>
          <p className="source-identity">
            {binding.catalog.name} · catalog #{binding.catalog.id}
          </p>
          <p className="helper inset-free">
            Missing catalog metadata:{' '}
            {binding.catalog.missing_metadata.join(', ').replaceAll('_', ' ') || 'Review source status'}.
          </p>
          {unavailableCatalog && (
            <p className="inline-error" role="alert">
              This record is absent from the complete active catalog. Its prices cannot be used. Refresh the catalog or
              select another source.
            </p>
          )}
          {changedCatalog && (
            <div className="inline-error" role="alert">
              <p>The catalog record changed. Its saved prices are stale.</p>
              <button
                type="button"
                className="secondary compact"
                onClick={() => onBindingChange({ companyId: companyId!, catalog: latestCatalog! })}
              >
                Use refreshed catalog record
              </button>
            </div>
          )}
          <label className="field-label" htmlFor={id + '-basis'}>
            <span>Price basis</span>
            <select
              id={id + '-basis'}
              className="assignment-select"
              value={selectedPrice}
              onChange={event => {
                const option =
                  event.target.value === '' ? undefined : binding.catalog.price_options[Number(event.target.value)];
                onBindingChange({
                  companyId: binding.companyId,
                  catalog: binding.catalog,
                  ...(option ? { priceBasis: option.price_basis, priceKey: option.price_key } : {}),
                });
              }}
            >
              <option value="">Select a price basis explicitly</option>
              {binding.catalog.price_options.map((option, index) => (
                <option key={index} value={index}>
                  {basisLabel[option.price_basis]}
                  {option.price_key ? ` · ${option.price_key}` : ''} · {option.unit_price ?? 'missing price'}
                </option>
              ))}
            </select>
          </label>
          <p className="helper inset-free">
            A gauge key is a selected catalog price row; it does not verify the entered thickness.
          </p>
          <button
            type="button"
            className="secondary compact"
            disabled={!companyId || !binding.priceBasis || resolving || unavailableCatalog}
            onClick={() => void resolve()}
          >
            {resolving ? 'Resolving source…' : 'Resolve sheet prices'}
          </button>
          {error && (
            <p className="inline-error" role="alert">
              {error}
            </p>
          )}
          {resolved && (
            <div className="source-resolution">
              <p>
                <strong>{current ? 'Calculated from selected source' : 'Saved or stale source snapshot'}</strong> · not
                approved material data
              </p>
              <dl>
                <dt>Source updated</dt>
                <dd>
                  {resolved.catalog_material.source_updated_at
                    ? formatCentralDateTime(resolved.catalog_material.source_updated_at)
                    : 'Not recorded'}
                </dd>
                <dt>Density lb/in³</dt>
                <dd>{resolved.catalog_material.density_lb_per_cubic_inch ?? 'Not recorded'}</dd>
                <dt>Currency</dt>
                <dd>Not recorded in catalog</dd>
              </dl>
              <details>
                <summary>Source, conversion and review details</summary>
                <p className="hash-value">Snapshot: {resolved.content_hash}</p>
                <p>
                  Source field:{' '}
                  {binding.catalog.price_options.find(
                    option =>
                      option.price_basis === binding.priceBasis && option.price_key === (binding.priceKey ?? null)
                  )?.source_field ?? 'Not selected'}
                </p>
                <ul>
                  {resolved.issues.map((issue, index) => (
                    <li key={index}>{issue.message}</li>
                  ))}
                </ul>
                <ul>
                  {resolved.stocks.map(stock => (
                    <li key={stock.id}>
                      {stock.width_in} × {stock.length_in} in: {stock.sheet_cost ?? 'unavailable'} currency units per
                      sheet; {stock.area_sq_ft} ft², {stock.volume_cu_in} in³
                      {stock.weight_lb ? `, ${stock.weight_lb} lb` : ''}.
                    </li>
                  ))}
                </ul>
              </details>
              {!applied && current && (
                <form
                  onSubmit={form.handleSubmit(() => {
                    if (!binding.resolution || !resolutionMatchesInputs(quote)) return;
                    onApply(
                      {
                        ...binding,
                        acknowledgement: {
                          currency: 'USD',
                          reviewed: true,
                          contentHash: binding.resolution.content_hash,
                        },
                      },
                      new Map(binding.resolution.stocks.map(stock => [stock.id, Number(stock.sheet_cost)]))
                    );
                  })}
                >
                  <label className="source-acknowledgement">
                    <input type="checkbox" {...form.register('reviewed')} />
                    <span>
                      I am using these values as USD and have reviewed the unresolved catalog metadata for this
                      estimate.
                    </span>
                  </label>
                  {form.formState.errors.reviewed && (
                    <p className="inline-error" role="alert">
                      {form.formState.errors.reviewed.message}
                    </p>
                  )}
                  <button className="primary compact" type="submit">
                    Apply reviewed USD prices
                  </button>
                </form>
              )}
              {applied && (
                <p className="helper inset-free">
                  USD is an estimator assumption. This is not an approved material, certification or inventory record.
                </p>
              )}
              {!current && (
                <p className="helper inset-free">
                  Resolve the current inputs before applying prices. Unresolved values are never replaced with zero or a
                  guessed density.
                </p>
              )}
            </div>
          )}
          <button type="button" className="text-button" onClick={() => onBindingChange()}>
            Clear catalog source and prices
          </button>
        </>
      )}
    </section>
  );
}
