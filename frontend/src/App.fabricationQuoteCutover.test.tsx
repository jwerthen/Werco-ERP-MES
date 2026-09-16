import { existsSync, readFileSync } from 'fs';
import { join } from 'path';

const appSource = readFileSync(join(__dirname, 'App.tsx'), 'utf8');
const navigation = readFileSync(join(__dirname, 'components/Layout.tsx'), 'utf8');
const client = readFileSync(join(__dirname, 'services/api.ts'), 'utf8');

it.each(['/nest', '/rfq-packages/new', '/quote-calculator', '/estimate-workbench', '/estimate-workbench/:estimateId', '/shop-data'])(
  'redirects the retired %s bookmark to fabrication quoting behind authentication', path => {
    const declaration = appSource.slice(appSource.indexOf(`path="${path}"`));
    expect(declaration.slice(0, declaration.indexOf('</PrivateRoute>'))).toMatch(/<PrivateRoute>\s*<Navigate to="\/fabrication-quotes" replace \/>/);
    expect(navigation).not.toContain(`href: '${path}'`);
  }
);

it('removes the executable legacy quoting pages, feature and client endpoints', () => {
  for (const page of ['RFQQuoting', 'QuoteCalculator', 'EstimateWorkbench', 'ShopData', 'MaterialNesting']) {
    expect(existsSync(join(__dirname, 'pages', `${page}.tsx`))).toBe(false);
    expect(appSource).not.toContain(`import('./pages/${page}')`);
  }
  expect(existsSync(join(__dirname, 'features/nesting'))).toBe(false);
  for (const endpoint of ['/quote-calc', '/quote-nesting', '/estimate-workbench', '/rfq-packages']) {
    expect(client).not.toContain(endpoint);
  }
  expect(client).toContain('/laser-nests/');
  expect(client).toContain('/work-orders/');
});
