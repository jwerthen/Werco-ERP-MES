/**
 * usePageTitle — the browser-tab title hook.
 *
 * Pins the three behaviors that matter: set on mount, follow changes, and
 * deliberately NOT restore on unmount (the next routed screen owns the title;
 * a cleanup would only flash the default between routes).
 */
import { renderHook } from '@testing-library/react';
import { usePageTitle } from './usePageTitle';

describe('usePageTitle', () => {
  afterEach(() => {
    document.title = '';
  });

  it('sets document.title on mount', () => {
    renderHook(() => usePageTitle('Work Orders · Werco ERP'));
    expect(document.title).toBe('Work Orders · Werco ERP');
  });

  it('follows the title when it changes', () => {
    const { rerender } = renderHook(({ title }) => usePageTitle(title), {
      initialProps: { title: 'Parts · Werco ERP' },
    });
    expect(document.title).toBe('Parts · Werco ERP');

    rerender({ title: 'Quotes · Werco ERP' });
    expect(document.title).toBe('Quotes · Werco ERP');
  });

  it('does not restore the previous title on unmount (next route overwrites)', () => {
    document.title = 'Werco ERP';
    const { unmount } = renderHook(() => usePageTitle('Wallboard · Werco ERP'));
    expect(document.title).toBe('Wallboard · Werco ERP');

    unmount();
    expect(document.title).toBe('Wallboard · Werco ERP');
  });
});
