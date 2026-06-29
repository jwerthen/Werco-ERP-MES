/**
 * useUnsavedChanges — beforeunload guard + Cancel/Close confirm gate.
 *
 * Covers:
 *   - beforeunload is registered only while dirty, and removed when clean /
 *     on unmount (so a saved form never blocks refresh/close),
 *   - the registered handler triggers the native prompt (preventDefault +
 *     returnValue), and
 *   - confirmDiscard() short-circuits true when clean and defers to
 *     window.confirm when dirty.
 *
 * In-app SPA-nav blocking is intentionally NOT covered: the app uses the
 * component <BrowserRouter>, so react-router's useBlocker is unavailable. See
 * the hook's doc comment.
 */

import { renderHook, act } from '@testing-library/react';
import { useUnsavedChanges } from './useUnsavedChanges';

describe('useUnsavedChanges', () => {
  let addSpy: jest.SpyInstance;
  let removeSpy: jest.SpyInstance;

  beforeEach(() => {
    addSpy = jest.spyOn(window, 'addEventListener');
    removeSpy = jest.spyOn(window, 'removeEventListener');
  });

  afterEach(() => {
    addSpy.mockRestore();
    removeSpy.mockRestore();
    jest.restoreAllMocks();
  });

  function beforeUnloadCalls(spy: jest.SpyInstance) {
    return spy.mock.calls.filter(([type]) => type === 'beforeunload');
  }

  it('does NOT register beforeunload when clean', () => {
    renderHook(() => useUnsavedChanges(false));
    expect(beforeUnloadCalls(addSpy)).toHaveLength(0);
  });

  it('registers beforeunload while dirty', () => {
    renderHook(() => useUnsavedChanges(true));
    expect(beforeUnloadCalls(addSpy)).toHaveLength(1);
  });

  it('removes the beforeunload listener when dirty flips to clean', () => {
    const { rerender } = renderHook(({ dirty }) => useUnsavedChanges(dirty), {
      initialProps: { dirty: true },
    });
    expect(beforeUnloadCalls(addSpy)).toHaveLength(1);

    rerender({ dirty: false });
    expect(beforeUnloadCalls(removeSpy)).toHaveLength(1);
  });

  it('removes the beforeunload listener on unmount', () => {
    const { unmount } = renderHook(() => useUnsavedChanges(true));
    expect(beforeUnloadCalls(addSpy)).toHaveLength(1);
    unmount();
    expect(beforeUnloadCalls(removeSpy)).toHaveLength(1);
  });

  it('the registered handler triggers the native prompt', () => {
    renderHook(() => useUnsavedChanges(true));
    const handler = beforeUnloadCalls(addSpy)[0][1] as (e: Event) => unknown;

    const event = new Event('beforeunload', { cancelable: true }) as BeforeUnloadEvent;
    const preventDefault = jest.spyOn(event, 'preventDefault');
    const returned = handler(event);

    // Both preventDefault() and a returned/assigned string are required across
    // browsers to trigger the native unsaved-changes prompt. (jsdom maps the
    // legacy Event.returnValue onto the cancelable flag, so we assert the
    // handler's return value rather than reading returnValue back.)
    expect(preventDefault).toHaveBeenCalled();
    expect(returned).toBe('');
  });

  describe('confirmDiscard', () => {
    it('returns true immediately when clean (no confirm shown)', () => {
      const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
      const { result } = renderHook(() => useUnsavedChanges(false));

      let allowed = false;
      act(() => {
        allowed = result.current.confirmDiscard();
      });

      expect(allowed).toBe(true);
      expect(confirmSpy).not.toHaveBeenCalled();
    });

    it('defers to window.confirm when dirty and returns its result', () => {
      const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
      const { result } = renderHook(() => useUnsavedChanges(true));

      let allowed = false;
      act(() => {
        allowed = result.current.confirmDiscard();
      });

      expect(confirmSpy).toHaveBeenCalledTimes(1);
      expect(allowed).toBe(true);
    });

    it('returns false when the user cancels the confirm', () => {
      jest.spyOn(window, 'confirm').mockReturnValue(false);
      const { result } = renderHook(() => useUnsavedChanges(true));

      let allowed = true;
      act(() => {
        allowed = result.current.confirmDiscard();
      });

      expect(allowed).toBe(false);
    });

    it('uses the custom message when prompting', () => {
      const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
      const { result } = renderHook(() => useUnsavedChanges(true, 'Toss it?'));

      act(() => {
        result.current.confirmDiscard();
      });

      expect(confirmSpy).toHaveBeenCalledWith('Toss it?');
    });
  });
});
