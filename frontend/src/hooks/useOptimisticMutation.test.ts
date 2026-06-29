import { renderHook, act, waitFor } from '@testing-library/react';
import { useOptimisticMutation } from './useOptimisticMutation';

// Capture toast calls at the module boundary so we can assert on success-vs-error.
const showToast = jest.fn();
jest.mock('../components/ui/Toast', () => ({
  __esModule: true,
  useToast: () => ({ showToast }),
}));

describe('useOptimisticMutation', () => {
  beforeEach(() => {
    showToast.mockClear();
  });

  it('applies the optimistic update synchronously, before mutate resolves', async () => {
    const order: string[] = [];
    const applyOptimistic = jest.fn(() => order.push('optimistic'));
    let resolveMutate: () => void = () => {};
    const mutate = jest.fn(
      () =>
        new Promise<void>((resolve) => {
          order.push('mutate-called');
          resolveMutate = () => {
            order.push('mutate-resolved');
            resolve();
          };
        })
    );

    const { result } = renderHook(() =>
      useOptimisticMutation<void>({ applyOptimistic, rollback: jest.fn(), mutate })
    );

    let runPromise: Promise<unknown>;
    act(() => {
      runPromise = result.current.run();
    });

    // Optimistic update ran synchronously, before the promise settles.
    expect(applyOptimistic).toHaveBeenCalledTimes(1);
    expect(order).toEqual(['optimistic', 'mutate-called']);
    expect(result.current.pending).toBe(true);

    await act(async () => {
      resolveMutate();
      await runPromise;
    });

    expect(order).toEqual(['optimistic', 'mutate-called', 'mutate-resolved']);
    expect(result.current.pending).toBe(false);
  });

  it('keeps the optimistic state on success, reconciles, and shows no error toast', async () => {
    const rollback = jest.fn();
    const reconcile = jest.fn();
    const serverRow = { id: 7, status: 'read' };
    const mutate = jest.fn().mockResolvedValue(serverRow);

    const { result } = renderHook(() =>
      useOptimisticMutation<typeof serverRow>({
        applyOptimistic: jest.fn(),
        rollback,
        mutate,
        reconcile,
      })
    );

    let returned: unknown;
    await act(async () => {
      returned = await result.current.run();
    });

    expect(returned).toBe(serverRow);
    // reconcile receives (result, ctx); ctx is undefined for a no-context run().
    expect(reconcile).toHaveBeenCalledWith(serverRow, undefined);
    expect(rollback).not.toHaveBeenCalled();
    expect(showToast).not.toHaveBeenCalled();
    expect(result.current.pending).toBe(false);
  });

  it('rolls back and toasts the verbatim server detail on failure (never success)', async () => {
    const rollback = jest.fn();
    const err = { response: { data: { detail: 'Work order is not release-ready.' } } };
    const mutate = jest.fn().mockRejectedValue(err);

    const { result } = renderHook(() =>
      useOptimisticMutation<void>({
        applyOptimistic: jest.fn(),
        rollback,
        mutate,
        errorFallback: 'Failed to update',
      })
    );

    let returned: unknown = 'sentinel';
    await act(async () => {
      returned = await result.current.run();
    });

    expect(returned).toBeUndefined();
    expect(rollback).toHaveBeenCalledTimes(1);
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(showToast).toHaveBeenCalledWith('error', 'Work order is not release-ready.');
    // Crucially, no success toast was ever emitted for the failed call.
    expect(showToast).not.toHaveBeenCalledWith('success', expect.anything());
    await waitFor(() => expect(result.current.pending).toBe(false));
  });

  it('falls back to err.message then errorFallback when no server detail is present', async () => {
    const { result: r1 } = renderHook(() =>
      useOptimisticMutation<void>({
        applyOptimistic: jest.fn(),
        rollback: jest.fn(),
        mutate: jest.fn().mockRejectedValue(new Error('Network Error')),
        errorFallback: 'Failed to update',
      })
    );
    await act(async () => {
      await r1.current.run();
    });
    expect(showToast).toHaveBeenLastCalledWith('error', 'Network Error');

    showToast.mockClear();
    const { result: r2 } = renderHook(() =>
      useOptimisticMutation<void>({
        applyOptimistic: jest.fn(),
        rollback: jest.fn(),
        mutate: jest.fn().mockRejectedValue({}),
        errorFallback: 'Failed to update',
      })
    );
    await act(async () => {
      await r2.current.run();
    });
    expect(showToast).toHaveBeenLastCalledWith('error', 'Failed to update');
  });

  it('threads a per-call ctx so an overlapping rollback restores the right target', async () => {
    // Two DIFFERENT targets actioned in quick succession; the EARLIER one is
    // rejected. Each run must roll back the ctx IT was invoked with — not a shared
    // ref that the later run overwrote. (Regression for the shared-ref desync.)
    const rolledBack: string[] = [];
    let rejectFirst: (err: unknown) => void = () => {};
    const mutate = jest.fn((ctx: { id: string }) => {
      if (ctx.id === 'A') {
        return new Promise<void>((_, reject) => {
          rejectFirst = reject;
        });
      }
      return Promise.resolve();
    });

    const { result } = renderHook(() =>
      useOptimisticMutation<void, { id: string }>({
        applyOptimistic: jest.fn(),
        rollback: (ctx) => rolledBack.push(ctx.id),
        mutate,
      })
    );

    let firstRun: Promise<unknown> = Promise.resolve();
    await act(async () => {
      firstRun = result.current.run({ id: 'A' }); // in-flight
      await result.current.run({ id: 'B' }); // resolves immediately, no rollback
    });

    await act(async () => {
      rejectFirst({ message: 'A failed' });
      await firstRun;
    });

    // Only A rolled back, and it rolled back A (not B, which the old shared ref held).
    expect(rolledBack).toEqual(['A']);
  });

  it('uses a custom onError instead of the default toast when provided', async () => {
    const onError = jest.fn();
    const rollback = jest.fn();
    const err = { response: { data: { detail: 'nope' } } };

    const { result } = renderHook(() =>
      useOptimisticMutation<void>({
        applyOptimistic: jest.fn(),
        rollback,
        mutate: jest.fn().mockRejectedValue(err),
        onError,
      })
    );

    await act(async () => {
      await result.current.run();
    });

    expect(rollback).toHaveBeenCalledTimes(1);
    // onError receives (err, ctx); ctx is undefined for a no-context run().
    expect(onError).toHaveBeenCalledWith(err, undefined);
    expect(showToast).not.toHaveBeenCalled();
  });
});
