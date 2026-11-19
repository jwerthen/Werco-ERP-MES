import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import api from '../services/api';
import type { OwnerOption } from '../types/ownership';

export type OwnerKind = 'customer' | 'supplier';

export interface OwnerList {
  /** `null` until the list has been asked for and has arrived. */
  options: OwnerOption[] | null;
  loading: boolean;
  error: string;
}

export interface OwnerDirectory {
  customers: OwnerList;
  vendors: OwnerList;
  /** Fetch a list the first time it is needed; a no-op once loaded or in flight. */
  ensure: (kind: OwnerKind) => void;
  /** Re-fetch after a failed load. */
  retry: (kind: OwnerKind) => void;
}

const EMPTY: OwnerList = { options: null, loading: false, error: '' };

/**
 * The customer and supplier lists behind the ownership pickers, fetched LAZILY — only
 * when a picker for that kind is actually opened — and at most once per screen.
 *
 * Lazy on purpose: the screens that carry an ownership picker (Inventory, Receiving,
 * Tool Management) load company-owned stock almost always, so fetching both lists on
 * mount would add two requests to every page load for a choice nobody made. One
 * directory per screen also means a multi-line delivery shares one fetch across all
 * of its lines.
 *
 * Customers come from `GET /customers/names` (active, not deleted — the selectable
 * set). Vendors come from `GET /purchasing/vendors?active_only=false`: an INACTIVE
 * vendor is still a valid owner of property it consigned (only a deleted one is not),
 * so the picker shows it, labelled inactive.
 */
export function useOwnerDirectory(): OwnerDirectory {
  const [lists, setLists] = useState<Record<OwnerKind, OwnerList>>({ customer: EMPTY, supplier: EMPTY });
  const started = useRef<Record<OwnerKind, boolean>>({ customer: false, supplier: false });
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(async (kind: OwnerKind, force: boolean) => {
    if (started.current[kind] && !force) return;
    started.current[kind] = true;
    setLists(previous => ({ ...previous, [kind]: { ...previous[kind], loading: true, error: '' } }));
    try {
      const options: OwnerOption[] =
        kind === 'customer'
          ? (await api.getCustomerNames()).map(customer => ({ id: customer.id, name: customer.name }))
          : ((await api.getVendors({ active_only: false })) as OwnerOption[]).map(vendor => ({
              id: vendor.id,
              name: vendor.name,
              code: vendor.code ?? null,
              is_active: vendor.is_active !== false,
            }));
      if (mounted.current) setLists(previous => ({ ...previous, [kind]: { options, loading: false, error: '' } }));
    } catch {
      started.current[kind] = false;
      if (mounted.current)
        setLists(previous => ({
          ...previous,
          [kind]: {
            options: previous[kind].options,
            loading: false,
            error: kind === 'customer' ? 'Could not load customers.' : 'Could not load suppliers.',
          },
        }));
    }
  }, []);

  const ensure = useCallback((kind: OwnerKind) => void load(kind, false), [load]);
  const retry = useCallback((kind: OwnerKind) => void load(kind, true), [load]);

  return useMemo(() => ({ customers: lists.customer, vendors: lists.supplier, ensure, retry }), [lists, ensure, retry]);
}
