import { useCallback, useRef, useState } from 'react';

/**
 * useInFlightIds — per-row "request in flight" tracking for a list where several rows
 * can each have their own request pending at once.
 *
 * A single `number | null` slot is the bug this replaces: actioning row B overwrites
 * row A's slot and re-enables A's buttons while A's request is still in flight (so a
 * second click sends a second request), and A's `finally` then clears B's state early.
 * Here each id stays in flight until ITS OWN request settles.
 *
 * `start(id)` returns false when that id is already in flight — a synchronous
 * double-click guard that holds even before React has re-rendered the disabled button.
 * Render from `ids` (state), never from the ref.
 */
export function useInFlightIds() {
  const pending = useRef(new Set<number>());
  const [ids, setIds] = useState<ReadonlySet<number>>(() => new Set<number>());

  const start = useCallback((id: number): boolean => {
    if (pending.current.has(id)) return false;
    pending.current.add(id);
    setIds(new Set(pending.current));
    return true;
  }, []);

  const finish = useCallback((id: number) => {
    if (!pending.current.delete(id)) return;
    setIds(new Set(pending.current));
  }, []);

  return { ids, start, finish };
}

export default useInFlightIds;
