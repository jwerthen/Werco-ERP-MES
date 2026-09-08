import React, { useEffect, useState, useMemo } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import api from '../services/api';
import { EntitySearchResponse, SEARCH_TYPES, SEARCH_TYPE_PATHS } from '../types/search';
import { Button, ErrorState, EmptyState } from '../components/ui';
import { FormField } from '../components/ui/FormField';
import { usePermissions } from '../hooks/usePermissions';
import { canAccessPath } from '../utils/routeAccess';
import { useCompany } from '../context/CompanyContext';

export default function SearchResults() {
  const { can } = usePermissions();
  const permitted = useMemo(() => SEARCH_TYPES.filter(([key]) => canAccessPath(SEARCH_TYPE_PATHS[key], can)), [can]);
  const permittedKey = permitted.map(([key]) => key).join(',');
  const [params, setParams] = useSearchParams();
  const q = params.get('q') || '';
  const type = params.get('type') || '';
  const offset = Math.max(0, Math.min(100000, Number(params.get('offset')) || 0));
  const [input, setInput] = useState(q);
  const [data, setData] = useState<EntitySearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  const { currentCompany } = useCompany();
  useEffect(() => setInput(q), [q]);
  useEffect(() => {
    let active = true;
    setData(null);
    setError(false);
    if (!q.trim()) {
      setLoading(false);
      return;
    }
    if (!permittedKey || (type && !permittedKey.split(',').includes(type))) {
      setError(true);
      setLoading(false);
      return;
    }
    setLoading(true);
    api
      .search(q, type || permittedKey, { offset, limit: 25 })
      .then(result => {
        if (active) setData(result);
      })
      .catch(() => {
        if (active) setError(true);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [q, type, offset, retry, currentCompany?.id, permittedKey]);
  const update = (query: string, kind: string, pageOffset = 0) => {
    const next = new URLSearchParams();
    if (query.trim()) next.set('q', query.trim());
    if (kind) next.set('type', kind);
    if (pageOffset) next.set('offset', String(pageOffset));
    setParams(next);
  };
  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-semibold">Search results</h1>
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={event => {
          event.preventDefault();
          update(input, type);
        }}
      >
        <FormField label="Search records">
          {field => (
            <input
              {...field}
              className="input"
              maxLength={100}
              value={input}
              onChange={event => setInput(event.target.value)}
            />
          )}
        </FormField>
        <FormField label="Record type">
          {field => (
            <select {...field} className="input" value={type} onChange={event => update(q, event.target.value)}>
              <option value="">All record types</option>
              {permitted.map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                  {data?.categories[key] !== undefined ? ` (${data.categories[key]})` : ''}
                </option>
              ))}
            </select>
          )}
        </FormField>
        <Button type="submit">Search</Button>
      </form>
      {loading && <p role="status">Searching records…</p>}
      {error && (
        <ErrorState
          title="Search unavailable"
          message="Your search is preserved. Try again."
          onRetry={() => setRetry(value => value + 1)}
        />
      )}
      {!q.trim() && (
        <EmptyState
          title="Find a record"
          description="Enter an identifier, retired part number, name, or related text."
        />
      )}
      {data && (
        <>
          <p role="status" className="text-sm text-fd-mute">
            {data.total === 0
              ? 'No matches'
              : data.results.length === 0
                ? `No results on this page. ${data.total} matches are available.`
                : `${offset + 1}–${offset + data.results.length} of ${data.total} matches`}
          </p>
          <ul className="divide-y divide-fd-line border border-fd-line">
            {data.results.map(result => (
              <li key={`${result.type}-${result.id}`}>
                <Link
                  to={result.url}
                  className="block p-4 hover:bg-fd-panel focus-visible:outline focus-visible:outline-2"
                >
                  <span className="font-medium text-fd-blue">{result.title}</span>
                  <span className="ml-3 text-xs text-fd-mute">
                    {SEARCH_TYPES.find(([key]) => key === result.type)?.[1] || result.type}
                  </span>
                  {result.subtitle && <p className="text-sm text-fd-mute">{result.subtitle}</p>}
                  {result.matched_alias && <p className="text-sm">Formerly {result.matched_alias}</p>}
                </Link>
              </li>
            ))}
          </ul>
          <nav aria-label="Search result pages" className="flex flex-wrap gap-3">
            {offset > 0 && data.results.length === 0 && (
              <Button variant="secondary" onClick={() => update(q, type)}>
                First page
              </Button>
            )}
            <Button
              variant="secondary"
              disabled={offset === 0}
              onClick={() => update(q, type, Math.max(0, offset - 25))}
            >
              Previous
            </Button>
            <Button variant="secondary" disabled={!data.has_more} onClick={() => update(q, type, offset + 25)}>
              Next
            </Button>
          </nav>
        </>
      )}
    </div>
  );
}
