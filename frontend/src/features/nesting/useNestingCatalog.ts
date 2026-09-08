import { useCallback, useEffect, useRef, useState } from 'react';
import api from '../../services/api';
import type { NestingCatalogMaterial } from '../../types/quoteNesting';
import { catalogMaterialSchema } from './lib/material-binding';

export function nestingApiMessage(error: unknown): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === 'string'
    ? detail
    : error instanceof Error
      ? error.message
      : 'Could not read the ERP catalog.';
}

export default function useNestingCatalog(companyId?: number) {
  const [items, setItems] = useState<NestingCatalogMaterial[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [hasLoaded, setHasLoaded] = useState(false);
  const [error, setError] = useState('');
  const controller = useRef<AbortController | null>(null);
  const load = useCallback(
    async (offset = 0) => {
      controller.current?.abort();
      if (!companyId) {
        setLoading(false);
        setError('');
        setHasLoaded(false);
        return;
      }
      const request = new AbortController();
      controller.current = request;
      setLoading(true);
      setError('');
      try {
        const page = await api.getNestingMaterials(offset, 200, request.signal);
        if (request.signal.aborted) return;
        const received = page.items.map(item => catalogMaterialSchema.parse(item));
        setItems(current =>
          offset ? [...current, ...received.filter(item => !current.some(old => old.id === item.id))] : received
        );
        setTotal(page.total);
        setHasLoaded(true);
      } catch (cause) {
        if (!request.signal.aborted) setError(nestingApiMessage(cause));
      } finally {
        if (!request.signal.aborted) setLoading(false);
      }
    },
    [companyId]
  );
  useEffect(() => {
    setItems([]);
    setTotal(0);
    setHasLoaded(false);
    void load();
    return () => controller.current?.abort();
  }, [load]);
  return {
    items,
    total,
    loading,
    error,
    complete: hasLoaded && !loading && !error && items.length >= total,
    refresh: () => void load(),
    loadMore: () => void load(items.length),
  };
}

export type NestingCatalogState = ReturnType<typeof useNestingCatalog>;
