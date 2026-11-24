/**
 * Floor screens never read or render the untied-nest figure.
 *
 * Recorded contract (material_tie_view): the dispatch board, the operator kiosk,
 * the crew station, the shop-floor screens and the wallboard render NOTHING for
 * an operation with no material tie. The untied-nest reads are office planning
 * screens (Work Order Detail and /laser-nests/untied). This guard fails the
 * moment a floor surface imports the notice or calls one of the four reads.
 */

import * as fs from 'fs';
import * as path from 'path';

const SRC = path.join(__dirname, '../..');

const FLOOR_FILES = [
  'pages/OperatorKiosk.tsx',
  'pages/CrewStationKiosk.tsx',
  'pages/DispatchBoard.tsx',
  'pages/ShopFloor.tsx',
  'pages/ShopFloorSimple.tsx',
  'pages/Wallboard.tsx',
];

const FLOOR_DIRS = ['components/kiosk', 'components/dispatch'];

const FORBIDDEN = [
  'NestTieSummaryNotice',
  'useWorkOrderNestTieSummary',
  'getWorkOrderNestTieSummary',
  'getUntiedLaserNests',
  'getLaserNestTieCoverage',
  'getUntiedSheetReport',
  'tieNestSheet',
  '/laser-nests/untied',
  'tie-summary',
];

function walk(dir: string): string[] {
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return walk(full);
    return /\.(ts|tsx)$/.test(entry.name) && !/\.test\.(ts|tsx)$/.test(entry.name) ? [full] : [];
  });
}

const floorSources = [
  ...FLOOR_FILES.map(rel => path.join(SRC, rel)),
  ...FLOOR_DIRS.flatMap(rel => walk(path.join(SRC, rel))),
];

describe('untied laser nests stay off the floor screens', () => {
  it('finds the floor sources it guards', () => {
    for (const rel of FLOOR_FILES) {
      expect(fs.existsSync(path.join(SRC, rel))).toBe(true);
    }
    expect(floorSources.length).toBeGreaterThan(FLOOR_FILES.length);
  });

  it.each(floorSources.map(file => [path.relative(SRC, file), file]))(
    '%s reads no untied-nest route and mounts no untied-nest notice',
    (_rel, file) => {
      const text = fs.readFileSync(file, 'utf8');
      const hits = FORBIDDEN.filter(token => text.includes(token));
      expect(hits).toEqual([]);
    }
  );
});
