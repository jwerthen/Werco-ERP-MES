import { useEffect, useState } from 'react';

/**
 * useDebouncedValue — returns a debounced copy of `value` that only updates after
 * `delayMs` has elapsed without `value` changing. Useful for search inputs so the
 * filtered query / API call doesn't fire on every keystroke.
 *
 * The pending timer is cleared whenever `value` (or `delayMs`) changes, and on
 * unmount, so a fast-changing value never leaks a stale update.
 *
 * Usage:
 *   const [query, setQuery] = useState('');
 *   const debouncedQuery = useDebouncedValue(query, 250);
 *   // effect/memo keyed on debouncedQuery, not query
 */
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState<T>(value);

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);

  return debounced;
}

export default useDebouncedValue;
