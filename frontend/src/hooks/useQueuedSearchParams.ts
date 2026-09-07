import { useCallback, useLayoutEffect, useRef } from 'react';
import {
  createSearchParams,
  SetURLSearchParams,
  useLocation,
  useNavigationType,
  useSearchParams,
} from 'react-router-dom';

/** Merge rapid control changes while a data-router navigation is still pending.
 * Unlike React setState, router search-param callbacks do not queue updates.
 * Committed Back/Forward navigation always replaces the pending local intent.
 */
export function useQueuedSearchParams() {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigationType = useNavigationType();
  const latest = useRef(params);
  const pending = useRef(new Set<string>());
  useLayoutEffect(() => {
    const key = params.toString();
    if (navigationType === 'POP' || !pending.current.has(key) || key === latest.current.toString()) {
      latest.current = params;
      pending.current.clear();
    }
  }, [params, location.key, navigationType]);
  const update: SetURLSearchParams = useCallback(
    (nextInit, options) => {
      const next = createSearchParams(
        typeof nextInit === 'function' ? nextInit(new URLSearchParams(latest.current)) : nextInit
      );
      latest.current = next;
      pending.current.add(next.toString());
      setParams(next, options);
    },
    [setParams]
  );
  return [params, update] as const;
}
