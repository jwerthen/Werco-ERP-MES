import { readFileSync } from 'fs';
import { join } from 'path';

// As with App.legacyDeepLinkRedirect.test.tsx, pin the actual route table's
// declarations instead of copying route JSX into a separate test router.
const source = readFileSync(join(__dirname, 'App.tsx'), 'utf8');

describe('Material Nesting route registration', () => {
  it('loads the native workspace in the authenticated ERP layout', () => {
    const route = source.match(/<Route\s+path="\/nest"\s+element=\{([\s\S]*?)\}\s*\/>/);
    expect(route).not.toBeNull();
    expect(route?.[1]).toMatch(
      /<PrivateRoute>\s*<Layout>\s*<LazyRoute>\s*<MaterialNesting\s*\/>\s*<\/LazyRoute>\s*<\/Layout>\s*<\/PrivateRoute>/
    );
    expect(
      /const MaterialNesting\s*=\s*lazyWithRetry\(\(\)\s*=>\s*import\(['"]\.\/pages\/MaterialNesting['"]\)\)/.test(
        source
      )
    ).toBe(true);
  });

  it('requires the same purchasing:view permission as the Material Nesting navigation entry', () => {
    expect(/\{\s*prefix:\s*['"]\/nest['"],\s*permission:\s*['"]purchasing:view['"]\s*\}/.test(source)).toBe(true);
  });
});
