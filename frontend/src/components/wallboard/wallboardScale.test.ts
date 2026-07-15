/**
 * Rem-scaling discipline guard (spec §5 / impl notes).
 *
 * The wallboard root scales via fontSize calc(100vh / 67.5) and EVERY size in
 * the tree must be rem (Tailwind arbitrary values like text-[4.5rem],
 * border-[0.0625rem]). One stray fixed-px class (text-xl, bare `border`)
 * reintroduces the 4K half-size bug — so this test scans the wallboard
 * sources instead of hoping a reviewer catches it.
 */

import fs from 'fs';
import path from 'path';

const wallboardComponentDir = __dirname;
const sources: string[] = [
  ...fs
    .readdirSync(wallboardComponentDir)
    .filter(name => name.endsWith('.tsx'))
    .map(name => path.join(wallboardComponentDir, name)),
  path.resolve(wallboardComponentDir, '../../pages/Wallboard.tsx'),
];

describe('wallboard rem-scaling discipline', () => {
  it('scans the expected sources', () => {
    expect(sources.length).toBeGreaterThanOrEqual(7);
  });

  it.each(sources.map(file => [path.basename(file), file]))(
    '%s has no fixed-px Tailwind size classes',
    (_name, file) => {
      const src = fs.readFileSync(file, 'utf8');
      // No preset text-size classes — the type scale is explicit rem values.
      expect(src).not.toMatch(/\btext-(xs|sm|base|lg|[2-9]?xl)\b/);
      // No bare `border` / `border-{t,r,b,l,x,y}` (1px fixed) — hairlines are
      // border-[0.0625rem] so they scale with the root.
      expect(src).not.toMatch(/["'\s`]border(?:-[trblxy])?(?=["'\s`])/);
    }
  );
});
