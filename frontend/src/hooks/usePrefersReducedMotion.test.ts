import { act, renderHook } from '@testing-library/react';
import { usePrefersReducedMotion } from './usePrefersReducedMotion';

/** A controllable MediaQueryList for one query; `setMatches` fires 'change' like a browser. */
function installMatchMedia(initial: boolean) {
  const original = window.matchMedia;
  const listeners = new Set<EventListener>();
  const queries: string[] = [];
  let matches = initial;
  const matchMedia = (media: string): MediaQueryList => {
    queries.push(media);
    return {
      get matches() {
        return matches;
      },
      media,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: (_type: string, listener: EventListenerOrEventListenerObject) => {
        if (typeof listener === 'function') listeners.add(listener);
      },
      removeEventListener: (_type: string, listener: EventListenerOrEventListenerObject) => {
        if (typeof listener === 'function') listeners.delete(listener);
      },
      dispatchEvent: () => true,
    };
  };
  // setupTests.ts defines matchMedia writable (not configurable): assign, never redefine.
  window.matchMedia = matchMedia;
  return {
    queries,
    listeners,
    setMatches(next: boolean) {
      matches = next;
      Array.from(listeners).forEach(listener => listener(new Event('change')));
    },
    restore() {
      window.matchMedia = original;
    },
  };
}

describe('usePrefersReducedMotion', () => {
  let media: ReturnType<typeof installMatchMedia>;
  afterEach(() => media.restore());

  it('reads the reduced-motion media query on first render', () => {
    media = installMatchMedia(true);
    const seen: boolean[] = [];
    const { result } = renderHook(() => {
      const reduced = usePrefersReducedMotion();
      seen.push(reduced);
      return reduced;
    });
    // The very first render already has it (FLIP and the drop animation read it at mount),
    // not only the re-render after the subscribing effect.
    expect(seen[0]).toBe(true);
    expect(result.current).toBe(true);
    expect(media.queries).toContain('(prefers-reduced-motion: reduce)');
  });

  it('catches a change that lands between the first render and the subscription', () => {
    media = installMatchMedia(false);
    const installed = window.matchMedia;
    let reads = 0;
    // The render reads "no preference"; by the time the effect subscribes the setting has
    // changed, and no 'change' event reaches a listener that did not exist yet.
    window.matchMedia = (query: string) => {
      reads += 1;
      if (reads === 2) media.setMatches(true);
      return installed(query);
    };
    const { result } = renderHook(() => usePrefersReducedMotion());
    expect(reads).toBe(2);
    expect(result.current).toBe(true);
  });

  it('follows the setting live and stops listening on unmount', () => {
    media = installMatchMedia(false);
    const { result, unmount } = renderHook(() => usePrefersReducedMotion());
    expect(result.current).toBe(false);
    act(() => media.setMatches(true));
    expect(result.current).toBe(true);
    act(() => media.setMatches(false));
    expect(result.current).toBe(false);
    expect(media.listeners.size).toBe(1);
    unmount();
    expect(media.listeners.size).toBe(0);
  });
});
