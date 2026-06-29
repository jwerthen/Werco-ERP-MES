import React from 'react';
import { render, act } from '@testing-library/react';
import { MemoryRouter, useNavigate } from 'react-router-dom';
import { useScrollRestoration } from './useScrollRestoration';

// A harness that mounts the hook ONCE and exposes the router's navigate(), so a
// single hook instance can be driven across a real route sequence (the only way
// to exercise the save-previous / restore-current logic, which keys off a ref
// that persists across location changes on the same instance).
let navigateRef: ((to: string) => void) | null = null;

function Harness() {
  useScrollRestoration();
  const navigate = useNavigate();
  navigateRef = navigate;
  return null;
}

const renderHarness = (initial: string) =>
  render(
    React.createElement(MemoryRouter, { initialEntries: [initial] }, React.createElement(Harness))
  );

const go = (to: string) => {
  act(() => {
    navigateRef?.(to);
  });
};

describe('useScrollRestoration', () => {
  let scrollToSpy: jest.SpyInstance;

  const setScrollY = (y: number) => {
    Object.defineProperty(window, 'scrollY', { value: y, configurable: true, writable: true });
  };

  beforeEach(() => {
    sessionStorage.clear();
    navigateRef = null;
    setScrollY(0);
    scrollToSpy = jest.spyOn(window, 'scrollTo').mockImplementation(() => {});
  });

  afterEach(() => {
    scrollToSpy.mockRestore();
  });

  it('scrolls to top on a forward nav to an unseen route', () => {
    renderHarness('/work-orders');
    expect(scrollToSpy).toHaveBeenLastCalledWith(0, 0);
  });

  it('saves the outgoing route position and restores it on return (back navigation)', () => {
    renderHarness('/list');
    expect(scrollToSpy).toHaveBeenLastCalledWith(0, 0); // /list unseen → top

    // Scroll down on /list, then navigate forward to /detail.
    setScrollY(640);
    scrollToSpy.mockClear();
    go('/detail');

    // /detail is unseen → top; and /list's 640 was persisted.
    expect(scrollToSpy).toHaveBeenLastCalledWith(0, 0);
    expect(sessionStorage.getItem('scrollpos:/list')).toBe('640');

    // Scroll down on /detail, then navigate back to /list.
    setScrollY(120);
    scrollToSpy.mockClear();
    go('/list');

    // /list restored to its saved 640; /detail's 120 persisted.
    expect(scrollToSpy).toHaveBeenLastCalledWith(0, 640);
    expect(sessionStorage.getItem('scrollpos:/detail')).toBe('120');
  });

  it('keys saved positions by pathname + search', () => {
    renderHarness('/warehouse?tab=inventory');
    setScrollY(300);
    scrollToSpy.mockClear();

    // Same pathname, different query string → a distinct key, restored to top.
    go('/warehouse?tab=receiving');
    expect(scrollToSpy).toHaveBeenLastCalledWith(0, 0);
    expect(sessionStorage.getItem('scrollpos:/warehouse?tab=inventory')).toBe('300');

    // Return to the inventory tab → its own saved position.
    setScrollY(90);
    scrollToSpy.mockClear();
    go('/warehouse?tab=inventory');
    expect(scrollToSpy).toHaveBeenLastCalledWith(0, 300);
  });

  it('is resilient when sessionStorage throws (best-effort, never breaks navigation)', () => {
    const getSpy = jest.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    const setSpy = jest.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });

    expect(() => {
      renderHarness('/list');
      go('/detail');
    }).not.toThrow();
    // Falls back to scroll-to-top since no position can be read.
    expect(scrollToSpy).toHaveBeenLastCalledWith(0, 0);

    getSpy.mockRestore();
    setSpy.mockRestore();
  });
});
