/**
 * App — legacy notification / QMS-evidence deep-link redirects.
 *
 * `notifications.link` rows already in production — and absolute URLs already
 * DELIVERED by email, which no data migration can ever reach — carry route
 * shapes that no longer exist. `vercel.json` rewrites every path to index.html,
 * so even a cold click from a mail client lands in the router. These redirects
 * are therefore a PERMANENT compatibility guarantee, and the seven shapes are
 * mirrored in `backend/app/services/notification_links.py::LEGACY_LINK_SHAPES`
 * with a backend test that parses App.tsx and fails if one stops resolving.
 *
 * Covers, per shape: the resulting location (path + query), that `replace` is
 * used so Back doesn't bounce between the legacy URL and its target, and that a
 * non-numeric or injection-shaped id degrades to the bare list page rather than
 * interpolating attacker-influenceable text into a query string.
 *
 * `LegacyDeepLinkRedirect` is rendered in a `MemoryRouter` harness rather than
 * mounting the whole `App` — App pulls in every lazy route plus AuthProvider,
 * CompanyProvider and the rest, none of which this behavior depends on.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation, useNavigationType } from 'react-router-dom';
import {
  LegacyDeepLinkRedirect,
  legacyId,
  resolveLegacyPurchasing,
  resolveLegacyQuality,
  resolveLegacyQuote,
  resolveLegacyCalibration,
  resolveLegacyShipping,
} from './App';

/**
 * Renders the current location — and the navigation TYPE, which is how
 * `replace` is asserted. (MemoryRouter keeps its own stack and never touches
 * window.history, so `window.history.back()` cannot be used here.)
 */
function LocationProbe() {
  const location = useLocation();
  const navigationType = useNavigationType();
  return (
    <>
      <div data-testid="location">{`${location.pathname}${location.search}`}</div>
      <div data-testid="nav-type">{navigationType}</div>
    </>
  );
}

/**
 * The REAL resolvers from App.tsx, imported — not re-declared here.
 *
 * An earlier version of this file kept a hand-copied mirror of each resolver.
 * That made every assertion below vacuous: the mirror and the real function
 * could disagree and the suite would still be green (it was — the calibration
 * row asserted `?filter=due` while App.tsx had already been corrected to land
 * unfiltered). A test that re-implements its subject tests nothing. The route
 * PATTERNS are still mirrored below, because those genuinely live in JSX that
 * cannot be imported; the backend guard test is what pins those to App.tsx.
 */
const resolvers = {
  purchasing: resolveLegacyPurchasing,
  quality: resolveLegacyQuality,
  calibration: resolveLegacyCalibration,
  quotes: resolveLegacyQuote,
  shipping: resolveLegacyShipping,
};

const renderAt = (initialPath: string) =>
  render(
    <MemoryRouter initialEntries={['/starting-point', initialPath]} initialIndex={1}>
      <LocationProbe />
      <Routes>
        <Route path="/purchasing/:poId" element={<LegacyDeepLinkRedirect resolve={resolvers.purchasing} />} />
        <Route path="/quality/:legacyTab/:legacyId" element={<LegacyDeepLinkRedirect resolve={resolvers.quality} />} />
        <Route path="/calibration/:equipmentId" element={<LegacyDeepLinkRedirect resolve={resolvers.calibration} />} />
        <Route path="/quotes/:quoteId" element={<LegacyDeepLinkRedirect resolve={resolvers.quotes} />} />
        <Route path="/shipping/:shipmentId" element={<LegacyDeepLinkRedirect resolve={resolvers.shipping} />} />
        {/* Stubs for the redirect TARGETS. Without these the harness would 404
            on a successful redirect and the fell-through assertion below would
            be meaningless. */}
        {['/purchasing', '/quality', '/calibration', '/quotes', '/warehouse'].map(path => (
          <Route key={path} path={path} element={<div data-testid="landed">landed</div>} />
        ))}
        <Route path="*" element={<div data-testid="fell-through">NotFound</div>} />
      </Routes>
    </MemoryRouter>,
  );

const landedAt = () => screen.getByTestId('location').textContent;

describe('legacy deep-link redirects', () => {
  // One row per LEGACY_LINK_SHAPES entry in notification_links.py.
  test.each([
    ['/purchasing/123', '/purchasing?po=123'],
    ['/quality/ncr/42', '/quality?tab=ncr'],
    ['/quality/car/42', '/quality?tab=car'],
    ['/quality/fai/8', '/quality?tab=fai&fai=8'],
    // Unfiltered on purpose — `?filter=due` provably cannot contain the equipment
    // the notification names. See notification_links.py :: CALIBRATION_LIST.
    ['/calibration/7', '/calibration'],
    ['/quotes/9', '/quotes?id=9'],
    ['/shipping/4', '/warehouse?tab=shipping'],
  ])('%s redirects to %s', (from, to) => {
    renderAt(from);
    expect(landedAt()).toBe(to);
    // It redirected rather than rendering the 404.
    expect(screen.queryByTestId('fell-through')).not.toBeInTheDocument();
  });

  test('an unknown quality sub-tab degrades to the plain Quality page', () => {
    renderAt('/quality/scrap/3');
    expect(landedAt()).toBe('/quality');
  });

  test('a FAI link with a non-numeric id degrades instead of building a broken query', () => {
    renderAt('/quality/fai/abc');
    expect(landedAt()).toBe('/quality');
  });
});

describe('legacy id sanitization', () => {
  test.each([
    ['/purchasing/abc', '/purchasing'],
    ['/purchasing/1&x=2', '/purchasing'],
    ['/purchasing/..', '/purchasing'],
    ['/quotes/abc', '/quotes'],
    ['/quotes/9&admin=1', '/quotes'],
  ])('%s degrades to %s rather than interpolating the raw segment', (from, to) => {
    renderAt(from);
    expect(landedAt()).toBe(to);
  });

  test('legacyId accepts only bare integers', () => {
    expect(legacyId('123')).toBe('123');
    expect(legacyId('0')).toBe('0');
    expect(legacyId('abc')).toBeNull();
    expect(legacyId('1&x=2')).toBeNull();
    expect(legacyId('1 ')).toBeNull();
    expect(legacyId('-1')).toBeNull();
    expect(legacyId('1.5')).toBeNull();
    expect(legacyId('')).toBeNull();
    expect(legacyId(undefined)).toBeNull();
  });

  test('a sanitized id never survives into the redirect target', () => {
    // The failure this guards: `/purchasing?po=1&x=2` would smuggle an extra
    // query param into the app from a crafted URL.
    renderAt('/purchasing/1&x=2');
    expect(landedAt()).not.toContain('x=2');
  });
});

describe('history behavior', () => {
  // Without `replace`, Back from the target would return to the legacy URL,
  // which immediately redirects forward again — an inescapable loop that costs
  // the user two Back presses per notification click.
  test.each(['/purchasing/123', '/quality/fai/8', '/calibration/7', '/quotes/9', '/shipping/4'])(
    '%s redirects with replace, not push',
    from => {
      renderAt(from);
      expect(screen.getByTestId('nav-type').textContent).toBe('REPLACE');
    },
  );
});

describe('the harness mirrors the real App.tsx route table', () => {
  // The resolver functions in App.tsx are module-private, so this suite
  // reimplements them. That duplication is only safe if the ROUTE PATHS stay in
  // lock-step, so read them out of the source and assert each one is declared.
  // (The backend's tests/test_notification_link_routes.py enforces the same
  // paths from the other side, against LEGACY_LINK_SHAPES.)
  const source: string = require('fs').readFileSync(require('path').join(__dirname, 'App.tsx'), 'utf8');

  test.each([
    '/purchasing/:poId',
    '/quality/:legacyTab/:legacyId',
    '/calibration/:equipmentId',
    '/quotes/:quoteId',
    '/shipping/:shipmentId',
  ])('App.tsx declares the legacy route %s', path => {
    expect(source).toContain(`path="${path}"`);
  });

  test('every legacy route is wrapped in PrivateRoute and uses LegacyDeepLinkRedirect', () => {
    const matches = source.match(/<LegacyDeepLinkRedirect[\s\S]*?\/>/g) || [];
    expect(matches).toHaveLength(5);
    // No legacy redirect may be reachable unauthenticated.
    const block = source.slice(source.indexOf('path="/purchasing/:poId"'), source.indexOf('path="/mrp"'));
    expect((block.match(/<PrivateRoute>/g) || []).length).toBe(5);
  });
});

describe('unrecognized shapes still 404', () => {
  // Deliberate: this is an ALLOWLIST of shapes we know were written. A generic
  // "/:anything/:id" fallback would guess at a destination, and guessing is
  // what produced the original bug.
  test.each(['/documents/5', '/customer-complaints/5', '/spc/5', '/maintenance/5'])(
    '%s falls through to NotFound',
    path => {
      renderAt(path);
      expect(screen.getByTestId('fell-through')).toBeInTheDocument();
    },
  );
});
