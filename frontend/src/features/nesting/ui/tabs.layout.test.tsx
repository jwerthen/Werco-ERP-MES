import React from 'react';
import { readFileSync } from 'fs';
import { join } from 'path';
import postcss, { type PluginCreator } from 'postcss';
import { Features, transform } from 'lightningcss';
import { render, screen } from '@testing-library/react';
import { Tabs, TabsContent, TabsList, TabsTrigger } from './tabs';
import MaterialNesting from '../../../pages/MaterialNesting';

// Use the same CommonJS plugin entry as postcss.config.js; Tailwind's exported
// types require a newer moduleResolution than this repository's test program.
const tailwindcss: PluginCreator<{ optimize: boolean }> = require('@tailwindcss/postcss');

jest.mock('../nesting.css?inline', () => '', { virtual: true });
jest.mock('../../../context/AuthContext', () => ({ useAuth: () => ({ user: { id: 7, company_id: 10 } }) }));
jest.mock('../../../context/CompanyContext', () => ({ useCompany: () => ({ currentCompany: { id: 10 } }) }));
const mockShowToast = jest.fn();
jest.mock('../../../components/ui/Toast', () => ({ useToast: () => ({ showToast: mockShowToast }) }));

// Compile actual utilities to check DOM/CSS contracts. Direct PostCSS resolves
// the legacy horizontal variant correctly, so this harness does not reproduce
// Vite's inline-CSS variant bug. That build path still needs browser coverage.
let layoutStyle: HTMLStyleElement;

beforeAll(async () => {
  const source = join(__dirname, '..', 'nesting.css');
  const built = await postcss([tailwindcss({ optimize: false })]).process(readFileSync(source, 'utf8'), {
    from: source,
  });
  // jsdom does not understand CSS nesting or cascade layers. Lower nesting,
  // then retain the real selectors and layout declarations in cascade order.
  // Responsive container sizing is exercised by browser QA, not jsdom.
  const flattened = transform({
    filename: source,
    code: Buffer.from(built.css),
    targets: { chrome: 100 << 16 },
    include: Features.Nesting,
  }).code.toString();
  const layout: string[] = [];
  postcss.parse(flattened).walkRules(rule => {
    if (rule.parent?.type !== 'root' && !(rule.parent?.type === 'atrule' && rule.parent.name === 'layer')) return;
    const declarations = rule.nodes.filter(
      node =>
        node.type === 'decl' &&
        ['display', 'flex-direction', 'min-width', 'flex-shrink', 'flex-wrap', 'padding', 'padding-bottom'].includes(
          node.prop
        )
    );
    if (declarations.length) layout.push(`${rule.selector}{${declarations.map(node => node.toString()).join(';')}}`);
  });
  layoutStyle = document.createElement('style');
  layoutStyle.textContent = layout.join('\n');
  document.head.append(layoutStyle);
}, 30000);

afterAll(() => layoutStyle?.remove());

function renderTabs(orientation?: 'horizontal' | 'vertical') {
  render(
    <Tabs defaultValue="workspace" orientation={orientation}>
      <TabsList>
        <TabsTrigger value="workspace">Quote workspace</TabsTrigger>
        <TabsTrigger value="stock">Stock sizes</TabsTrigger>
      </TabsList>
      <TabsContent value="workspace">A wide sheet drawing and material settings</TabsContent>
      <TabsContent value="stock">Stock sizes and prices</TabsContent>
    </Tabs>
  );
  const list = screen.getByRole('tablist');
  const root = list.parentElement;
  if (!root) throw new Error('Tabs did not render their root.');
  return { root, list, panel: screen.getByRole('tabpanel') };
}

describe('nesting tabs against their generated layout CSS', () => {
  it('stacks the default horizontal tab navigation above a shrinkable content panel', () => {
    const { root, list, panel } = renderTabs();
    expect(root).toHaveAttribute('data-orientation', 'horizontal');
    expect(list.getAttribute('aria-orientation') ?? 'horizontal').toBe('horizontal');
    expect(getComputedStyle(root).display).toBe('flex');
    expect(getComputedStyle(root).flexDirection).toBe('column');
    // jsdom preserves Tailwind's mathematically-zero calc instead of resolving
    // custom properties. A missing min-width rule would return an empty string.
    expect(getComputedStyle(root).minWidth).toBe('calc(var(--spacing) * 0)');
    expect(getComputedStyle(panel).minWidth).toBe('calc(var(--spacing) * 0)');
  });

  it('forwards vertical orientation to Base UI and matches the vertical list utility selector', () => {
    const { root, list, panel } = renderTabs('vertical');
    expect(root).toHaveAttribute('data-orientation', 'vertical');
    expect(list).toHaveAttribute('aria-orientation', 'vertical');
    expect(getComputedStyle(root).flexDirection).toBe('row');
    expect(getComputedStyle(list).flexDirection).toBe('column');
    expect(getComputedStyle(panel).minWidth).toBe('calc(var(--spacing) * 0)');
  });

  it('matches the rendered workspace selectors for wrapping, shrinking, and settings spacing', () => {
    const rendered = render(<MaterialNesting />);
    const mount = screen.getByTestId('material-nesting-host').shadowRoot?.querySelector('[data-nesting-mount]');
    if (!mount) throw new Error('Material Nesting did not mount its workspace.');
    // jsdom has no ShadowRoot style cascade. Clone the actual rendered markup
    // into light DOM to test selector matching with the generated stylesheet;
    // the browser checks remain responsible for actual container dimensions.
    const styledWorkspace = mount.cloneNode(true) as HTMLElement;
    rendered.container.append(styledWorkspace);
    const element = (selector: string) => {
      const result = styledWorkspace.querySelector<HTMLElement>(selector);
      if (!result) throw new Error(`Workspace is missing ${selector}.`);
      return result;
    };
    expect(getComputedStyle(element('.nav-row')).minWidth).toBe('0');
    expect(getComputedStyle(element('.nav-row')).flexShrink).toBe('0');
    expect(getComputedStyle(element('.job-heading')).flexWrap).toBe('wrap');
    expect(getComputedStyle(element('.job-heading > div:first-child')).minWidth).toBe('0');
    // A malformed `.settings-:host` selector silently loses both these rules.
    expect(getComputedStyle(element('.quote-settings .settings-body')).paddingLeft).toBe('15px');
    expect(getComputedStyle(element('.quote-settings .settings-body')).paddingBottom).toBe('10px');
  });
});
