/**
 * PARITY: the Approved Supplier List vocabulary exists in two languages.
 *
 * `backend/app/services/supplier_approval_service.py` OWNS it — it normalizes every label to
 * a slug and 422s anything else — and `utils/supplierApproval.ts` transcribes the labels so
 * the ASL screens can name what the server sends and offer exactly the QL 7.4 dropdown values.
 * A drift would only mislabel (the server still refuses a bad value), but a mislabeled
 * controlled list is exactly the kind of quiet wrongness an auditor finds first.
 *
 * So this test READS the Python source and holds the transcription to it: the same slugs, in
 * the same (enum) order, with the same labels — plus the two status sets the screens branch on.
 * When it fails, change the TypeScript to match the Python, never the other way round.
 */
import * as fs from 'fs';
import * as path from 'path';
import {
  APPROVED_STATUSES,
  ASL_BASIS_LABELS,
  ASL_SCOPE_LABELS,
  ASL_STATUS_LABELS,
  REEVALUATION_STATUSES,
} from './supplierApproval';

const SOURCE = fs.readFileSync(
  path.join(__dirname, '../../../backend/app/services/supplier_approval_service.py'),
  'utf8'
);

/** `class Name(str, enum.Enum):` body -> ordered [[MEMBER, value], ...]. */
function enumMembers(className: string): Array<[string, string]> {
  const start = SOURCE.indexOf(`class ${className}(str, enum.Enum):`);
  expect(start).toBeGreaterThanOrEqual(0);
  const rest = SOURCE.slice(start).split('\n').slice(1);
  const members: Array<[string, string]> = [];
  for (const line of rest) {
    if (line.trim() === '' || line.trim().startsWith('"""') || line.trim().startsWith('#')) continue;
    const match = line.match(/^ {4}([A-Z0-9_]+) = "([^"]+)"/);
    if (!match) {
      if (/^\S/.test(line)) break; // dedent: the class body ended
      continue;
    }
    members.push([match[1], match[2]]);
  }
  return members;
}

/** `NAME: Dict[str, str] = { Enum.MEMBER.value: "Label", ... }` -> ordered [[slug, label]]. */
function labelMap(dictName: string, className: string): Array<[string, string]> {
  const values = new Map(enumMembers(className));
  const start = SOURCE.indexOf(`${dictName}: Dict[str, str] = {`);
  expect(start).toBeGreaterThanOrEqual(0);
  const body = SOURCE.slice(start, SOURCE.indexOf('\n}', start));
  const entries: Array<[string, string]> = [];
  const pattern = new RegExp(`${className}\\.([A-Z0-9_]+)\\.value: "([^"]+)"`, 'g');
  for (let match = pattern.exec(body); match; match = pattern.exec(body)) {
    const slug = values.get(match[1]);
    expect(slug).toBeDefined();
    entries.push([slug as string, match[2]]);
  }
  return entries;
}

/** A frozenset({...}) of enum members -> sorted slugs. */
function statusSet(name: string): string[] {
  const values = new Map(enumMembers('ApprovalStatus'));
  const start = SOURCE.indexOf(`${name}: FrozenSet[str] = frozenset(`);
  expect(start).toBeGreaterThanOrEqual(0);
  const body = SOURCE.slice(start, SOURCE.indexOf(')\n', start));
  return Array.from(body.matchAll(/ApprovalStatus\.([A-Z_]+)\.value/g), match => values.get(match[1]) as string).sort();
}

describe('ASL vocabulary parity with supplier_approval_service.py', () => {
  it('statuses: same slugs, order and labels', () => {
    const python = labelMap('STATUS_LABELS', 'ApprovalStatus');
    expect(python).toHaveLength(4);
    expect(Object.entries(ASL_STATUS_LABELS)).toEqual(python);
    expect(Object.keys(ASL_STATUS_LABELS)).toEqual(enumMembers('ApprovalStatus').map(([, value]) => value));
  });

  it('scopes of supply: the 19 dropdown values in enum order', () => {
    const python = labelMap('SCOPE_LABELS', 'AslScope');
    expect(python).toHaveLength(19);
    expect(Object.keys(ASL_SCOPE_LABELS)).toEqual(enumMembers('AslScope').map(([, value]) => value));
    expect(Object.entries(ASL_SCOPE_LABELS).sort()).toEqual([...python].sort());
  });

  it('approval bases: the 7 dropdown values in enum order', () => {
    const python = labelMap('BASIS_LABELS', 'ApprovalBasis');
    expect(python).toHaveLength(7);
    expect(Object.keys(ASL_BASIS_LABELS)).toEqual(enumMembers('ApprovalBasis').map(([, value]) => value));
    expect(Object.entries(ASL_BASIS_LABELS).sort()).toEqual([...python].sort());
  });

  it('the approved and re-evaluation status sets match', () => {
    expect(Array.from(APPROVED_STATUSES).sort()).toEqual(statusSet('APPROVED_STATUSES'));
    expect(Array.from(REEVALUATION_STATUSES).sort()).toEqual(statusSet('REEVALUATION_STATUSES'));
  });
});
