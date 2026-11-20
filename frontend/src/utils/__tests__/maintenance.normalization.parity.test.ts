/**
 * PARITY: checklist item normalization exists in two languages.
 *
 * `normalize_checklist_item` / `checklist_item_key` in `backend/app/services/maintenance_checklist.py`
 * decide which items a completion leaves unticked; `normalizeChecklistItem` / `checklistItemKey` in
 * `utils/maintenance.ts` decide which rows the Complete dialog shows and which ticks it carries. If
 * the two disagree, the dialog lists an item the server does not (or merges two the server counts
 * apart), and the technician is refused over a step they cannot see.
 *
 * The shared fixture is generated from the Python implementation and run by pytest too
 * (`backend/tests/test_maintenance_checklist_normalization_parity.py`). Add a case there, never in
 * only one language — and never edit the fixture to make one side pass.
 */
import * as fs from 'fs';
import * as path from 'path';
import { checklistItemKey, normalizeChecklistItem } from '../maintenance';

interface NormalizationCase {
  input: string;
  normalized: string;
  key: string;
}

const FIXTURE: { cases: NormalizationCase[] } = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'fixtures/maintenance-checklist-normalization.json'), 'utf8')
);

/** A case's input as code points, so a failure names an invisible character. */
const visible = (text: string) =>
  Array.from(text)
    .map(ch => (/[ -~]/.test(ch) ? ch : `\\u${ch.codePointAt(0)?.toString(16).padStart(4, '0')}`))
    .join('');

describe('checklist item normalization matches the Python twin', () => {
  it('has cases to check', () => {
    expect(FIXTURE.cases.length).toBeGreaterThan(20);
  });

  it.each(FIXTURE.cases.map(entry => [visible(entry.input), entry] as const))(
    'normalizes %s the same way',
    (_label, entry) => {
      expect(normalizeChecklistItem(entry.input)).toBe(entry.normalized);
      expect(checklistItemKey(entry.input)).toBe(entry.key);
    }
  );

  it.each(FIXTURE.cases.map(entry => [visible(entry.normalized), entry] as const))(
    'is idempotent on %s',
    (_label, entry) => {
      expect(normalizeChecklistItem(entry.normalized)).toBe(entry.normalized);
      expect(checklistItemKey(entry.normalized)).toBe(entry.key);
    }
  );

  it('reads a JSON scalar as the page does (String(value ?? ""))', () => {
    expect(normalizeChecklistItem(null)).toBe('');
    expect(normalizeChecklistItem(undefined)).toBe('');
    expect(normalizeChecklistItem(42)).toBe('42');
    expect(normalizeChecklistItem(true)).toBe('true');
  });
});
