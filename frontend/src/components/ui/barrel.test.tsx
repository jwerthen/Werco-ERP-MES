/**
 * UI barrel (`components/ui/index.ts`) — import smoke test.
 *
 * Batch 1 surfaced StatusBadge, Tabs, ConfirmDialog, and Breadcrumbs through the
 * barrel so callers can import them from '../components/ui' alongside the rest of
 * the kit. This guards that those names stay re-exported and are real React
 * components (a broken/renamed export would surface as `undefined` here, not at
 * some distant call site).
 */

import * as UI from './index';
import { StatusBadge, Tabs, ConfirmDialog, Breadcrumbs, UnitBadge } from './index';

describe('components/ui barrel exports', () => {
  it('re-exports StatusBadge, Tabs, ConfirmDialog, and Breadcrumbs as functions', () => {
    expect(typeof UI.StatusBadge).toBe('function');
    expect(typeof UI.Tabs).toBe('function');
    expect(typeof UI.ConfirmDialog).toBe('function');
    expect(typeof UI.Breadcrumbs).toBe('function');
  });

  it('re-exports UnitBadge as a function', () => {
    // 083. Ten callers across the app, the kiosk and the dispatch board import it from
    // this barrel; a renamed or dropped export would surface as `undefined` at each of
    // those call sites (React renders nothing for an undefined element type in
    // production) rather than here.
    expect(typeof UI.UnitBadge).toBe('function');
  });

  it('named imports resolve to the same components', () => {
    expect(StatusBadge).toBe(UI.StatusBadge);
    expect(Tabs).toBe(UI.Tabs);
    expect(ConfirmDialog).toBe(UI.ConfirmDialog);
    expect(Breadcrumbs).toBe(UI.Breadcrumbs);
    expect(UnitBadge).toBe(UI.UnitBadge);
  });
});
