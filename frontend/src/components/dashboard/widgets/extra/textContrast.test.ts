/**
 * Guard: the opt-in panels sit on `bg-fd-panel` (#141b26), where `text-slate-500` measures
 * 3.63:1 — below the WCAG 1.4.3 AA minimum of 4.5:1 for the 10–14 px labels, hints,
 * timestamps and "n/a" values these panels render. Small secondary text uses
 * `text-slate-400` (6.58:1), the EmptyState precedent. Darker slates stay available for
 * non-text decoration (borders, icons), which this scan does not touch.
 */
import fs from 'fs';
import path from 'path';

const LOW_CONTRAST_TEXT = /\btext-slate-(?:500|600|700)\b/g;

function panelSources(): string[] {
  return fs
    .readdirSync(__dirname)
    .filter(name => /\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name))
    .map(name => path.join(__dirname, name));
}

describe('opt-in dashboard panels: text contrast', () => {
  it('scans the panel sources', () => {
    expect(panelSources().length).toBeGreaterThanOrEqual(10);
  });

  it('never colors text slate-500 or darker on the panel surface', () => {
    const offenders = panelSources().flatMap(file =>
      fs
        .readFileSync(file, 'utf8')
        .split('\n')
        .flatMap((line, index) =>
          (line.match(LOW_CONTRAST_TEXT) ?? []).map(match => `${path.basename(file)}:${index + 1} ${match}`)
        )
    );
    expect(offenders).toEqual([]);
  });
});
