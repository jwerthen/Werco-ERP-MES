import { act, renderHook } from '@testing-library/react';
import { useInFlightIds } from './useInFlightIds';

describe('useInFlightIds', () => {
  it('tracks each id independently and refuses a second start of the same id', () => {
    const { result } = renderHook(() => useInFlightIds());

    let first = false;
    let again = true;
    act(() => {
      first = result.current.start(1);
      again = result.current.start(1);
      result.current.start(2);
    });
    expect(first).toBe(true);
    expect(again).toBe(false); // synchronous double-click guard
    expect(Array.from(result.current.ids).sort()).toEqual([1, 2]);

    // Finishing one id never clears another's in-flight state.
    act(() => result.current.finish(2));
    expect(result.current.ids.has(1)).toBe(true);
    expect(result.current.ids.has(2)).toBe(false);

    act(() => result.current.finish(1));
    expect(result.current.ids.size).toBe(0);
    let restart = false;
    act(() => {
      restart = result.current.start(1);
    });
    expect(restart).toBe(true);
  });
});
