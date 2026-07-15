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
    // Every wallboard component is scanned (new files opt IN automatically);
    // colocated test files are assertions, not rendered sources.
    .filter(name => name.endsWith('.tsx') && !name.endsWith('.test.tsx'))
    .map(name => path.join(wallboardComponentDir, name)),
  path.resolve(wallboardComponentDir, '../../pages/Wallboard.tsx'),
];

describe('wallboard rem-scaling discipline', () => {
  it('scans the expected sources (incl. the JobWall/JobTile job wall)', () => {
    // 9 components (ExceptionRail, FloorGrid, IdleStrip, JobRow, JobTile,
    // JobWall, TodayBand, WallboardHeader, WorkCenterTile) + the page.
    expect(sources.length).toBeGreaterThanOrEqual(9);
    expect(sources.some(file => file.endsWith('JobWall.tsx'))).toBe(true);
    expect(sources.some(file => file.endsWith('JobTile.tsx'))).toBe(true);
    expect(sources.some(file => file.endsWith('.test.tsx'))).toBe(false);
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
