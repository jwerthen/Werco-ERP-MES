import { renderHook, act } from '@testing-library/react';
import { useDebouncedValue } from './useDebouncedValue';

describe('useDebouncedValue', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('returns the initial value immediately', () => {
    const { result } = renderHook(() => useDebouncedValue('a', 250));
    expect(result.current).toBe('a');
  });

  it('only updates after the delay has elapsed', () => {
    const { result, rerender } = renderHook(({ value }) => useDebouncedValue(value, 250), {
      initialProps: { value: 'a' },
    });

    rerender({ value: 'ab' });
    // Not yet — delay has not elapsed.
    expect(result.current).toBe('a');

    act(() => {
      jest.advanceTimersByTime(249);
    });
    expect(result.current).toBe('a');

    act(() => {
      jest.advanceTimersByTime(1);
    });
    expect(result.current).toBe('ab');
  });

  it('coalesces rapid changes, emitting only the final value', () => {
    const { result, rerender } = renderHook(({ value }) => useDebouncedValue(value, 250), {
      initialProps: { value: 'a' },
    });

    rerender({ value: 'ab' });
    act(() => {
      jest.advanceTimersByTime(100);
    });
    rerender({ value: 'abc' });
    act(() => {
      jest.advanceTimersByTime(100);
    });
    rerender({ value: 'abcd' });

    // Still the original — each change reset the timer.
    expect(result.current).toBe('a');

    act(() => {
      jest.advanceTimersByTime(250);
    });
    expect(result.current).toBe('abcd');
  });

  it('clears the pending timer on unmount (no late update)', () => {
    const clearSpy = jest.spyOn(global, 'clearTimeout');
    const { rerender, unmount } = renderHook(({ value }) => useDebouncedValue(value, 250), {
      initialProps: { value: 'a' },
    });

    rerender({ value: 'b' });
    unmount();

    expect(clearSpy).toHaveBeenCalled();
    clearSpy.mockRestore();
  });
});
