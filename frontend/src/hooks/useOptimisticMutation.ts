import { useCallback, useRef, useState } from 'react';
import { useToast } from '../components/ui/Toast';

/**
 * useOptimisticMutation — generalizes the optimistic-update + rollback pattern
 * first written inline in AIEgressTab (apply the UI change immediately, await the
 * server, roll the UI back and surface the server's verbatim error on failure).
 *
 * The runner it returns:
 *  1. applies the optimistic state update *synchronously* before any await, so the
 *     UI reflects the intended end-state immediately;
 *  2. awaits `mutate()`;
 *  3. on success keeps the optimistic state (and, if `reconcile` is provided, hands
 *     it the server response so the page can replace the optimistic guess with the
 *     authoritative row);
 *  4. on failure calls `rollback()` AND surfaces the error — by default an error
 *     toast carrying the verbatim server `detail` (err.response?.data?.detail),
 *     falling back to err.message and then `errorFallback`.
 *
 * It NEVER shows success for a failed call: success side effects live only on the
 * resolved path, the error path only rolls back + reports.
 *
 * SAFE to adopt for mutations that are rarely server-rejected (mark-read / dismiss,
 * notification toggles, the egress kill switch). For server-GATED mutations whose
 * whole point is that the server may refuse — WO release-readiness, shop-floor
 * sequence/predecessor gating — do NOT make the UI optimistic; keep a loading state
 * and only reflect the result the server actually returns.
 *
 * Usage:
 *   const { run, pending } = useOptimisticMutation<Item>({
 *     applyOptimistic: () => setItems((prev) => prev.filter((i) => i.id !== id)),
 *     rollback:        () => setItems((prev) => [...prev, item]),
 *     mutate:          () => api.markRead(id),
 *     errorFallback:   'Failed to mark as read',
 *     // optional: reconcile: (server) => setItems((prev) => replace(prev, server)),
 *     // optional: onError: (err) => { ... } to suppress/customize the default toast
 *   });
 *   // in a handler: run();  // or: await run();
 *
 * Per-call context (`Ctx`): when ONE hook instance is shared across many rows/cards
 * (a single Delete handler for every WO row), pass the per-action target into
 * `run(ctx)` and have each callback take `(ctx)`. Each `run` closes over its own
 * `ctx`, so an in-flight rollback always restores the row THAT call acted on —
 * even if a second, different row was actioned before the first one was rejected.
 * Do NOT thread the target through a shared mutable ref: a later call overwrites it
 * and the earlier call's rollback restores the wrong row.
 *
 *   const { run } = useOptimisticMutation<unknown, { row: Row; index: number }>({
 *     applyOptimistic: (ctx) => setRows((prev) => prev.filter((r) => r.id !== ctx.row.id)),
 *     rollback:        (ctx) => setRows((prev) => insertAt(prev, ctx.row, ctx.index)),
 *     mutate:          (ctx) => api.deleteRow(ctx.row.id),
 *   });
 *   // in a handler: run({ row, index });
 */

const serverErrorDetail = (err: unknown, fallback: string): string => {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  const message = (err as { message?: unknown })?.message;
  if (typeof message === 'string' && message.trim()) return message;
  return fallback;
};

export interface UseOptimisticMutationOptions<T, Ctx = void> {
  /** Apply the optimistic UI change. Runs synchronously, before `mutate()` is awaited. */
  applyOptimistic: (ctx: Ctx) => void;
  /** Undo the optimistic change. Runs only when `mutate()` rejects. */
  rollback: (ctx: Ctx) => void;
  /** The server call. Its resolved value is passed to `reconcile` (if provided). */
  mutate: (ctx: Ctx) => Promise<T>;
  /** Optional: reconcile the kept optimistic state with the authoritative server response. */
  reconcile?: (result: T, ctx: Ctx) => void;
  /**
   * Optional: handle the error yourself. When provided it REPLACES the default
   * error toast (rollback still happens first). Return nothing; throwing is swallowed.
   */
  onError?: (err: unknown, ctx: Ctx) => void;
  /** Fallback message when the server provides no `detail`/`message`. */
  errorFallback?: string;
}

export interface UseOptimisticMutationResult<T, Ctx = void> {
  /**
   * Fire the optimistic mutation. Resolves to the server result, or `undefined` on
   * failure. The per-call `ctx` is threaded to every callback, so each run rolls back
   * the row IT acted on (no shared-ref desync across overlapping runs).
   */
  run: (ctx: Ctx) => Promise<T | undefined>;
  /** True while `mutate()` is in flight. */
  pending: boolean;
}

export function useOptimisticMutation<T = unknown, Ctx = void>(
  options: UseOptimisticMutationOptions<T, Ctx>
): UseOptimisticMutationResult<T, Ctx> {
  const { showToast } = useToast();
  const [pending, setPending] = useState(false);

  // Keep the latest options in a ref so `run` is stable across renders and always
  // closes over the current callbacks (which typically capture fresh component state).
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const run = useCallback(async (ctx: Ctx): Promise<T | undefined> => {
    const { applyOptimistic, rollback, mutate, reconcile, onError, errorFallback } = optionsRef.current;

    // 1. Apply the optimistic update synchronously, before awaiting anything.
    applyOptimistic(ctx);
    setPending(true);
    try {
      // 2. Await the server.
      const result = await mutate(ctx);
      // 3. Success: keep the optimistic state; optionally reconcile with the server.
      reconcile?.(result, ctx);
      return result;
    } catch (err) {
      // 4. Failure: roll back, then surface the error. Never a success toast.
      rollback(ctx);
      if (onError) {
        try {
          onError(err, ctx);
        } catch {
          // A throwing onError must not mask the original failure.
        }
      } else {
        showToast('error', serverErrorDetail(err, errorFallback || 'Action failed'));
      }
      return undefined;
    } finally {
      setPending(false);
    }
  }, [showToast]);

  return { run, pending };
}

export default useOptimisticMutation;
