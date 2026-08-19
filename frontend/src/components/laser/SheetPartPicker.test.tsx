/**
 * SheetPartPicker — the three tiers, and the one row that is never filtered.
 *
 * The picker answers three separate questions about a material part, and this
 * file exists because the first two used to be one:
 *
 *  1. MAY it be tied at all? A part the shop PRODUCES (`manufactured` /
 *     `assembly`) may not, at either tier and with no escape hatch — tying a
 *     work order to its own output and depleting it from stock at completion is
 *     not a preference, and consumption never auto-reverses.
 *  2. Is it in the DEFAULT view? Only raw stock that also reads as flat stock.
 *     `isSheetLikePart` is text-only, so "Sheet metal screw #8", "Plate nut" and
 *     "Abrasive sheets 9x11" all passed it and landed in a sheet picker; adding
 *     `isRawStockPartType` is what moves them behind the toggle. The escape
 *     hatch has to stay, because real sheet stock IS sometimes typed
 *     `purchased` (the BOM importer and PO upload both fall back to it).
 *  3. Is it the CURRENT SELECTION? Then it is listed regardless of tier 2 — a
 *     tie the planner already made must never be filtered out from under them
 *     and silently re-render as blank, because a blank picker on a re-import is
 *     exactly how a work order gets quietly untied.
 *
 * The toggle's count is asserted against what the toggle actually reveals
 * rather than against a literal, so a count that starts advertising rows that
 * are already on screen (a pinned selection, an appended extra option) fails
 * here instead of shipping.
 */

import React, { useState } from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { SheetPartPicker } from './SheetPartPicker';
import { ComboBoxOption } from '../ui/ComboBox';
import { Part } from '../../types';
import { comboBoxListbox, comboBoxOptions, openComboBox } from '../../test-utils/comboBox';

const part = (overrides: Partial<Part>): Part => ({
  id: 1,
  version: 1,
  part_number: 'MAT-1',
  revision: 'A',
  name: 'Material',
  part_type: 'raw_material',
  unit_of_measure: 'EA',
  standard_cost: 0,
  is_critical: false,
  requires_inspection: false,
  backflush_components: false,
  is_active: true,
  status: 'active',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  ...overrides,
});

/** Raw stock, numbered on the shop's convention — the default tier. */
const RAW_PLATE = part({ id: 41, part_number: '0.188-72X144-A36', name: 'A36 HR plate', part_type: 'raw_material' });

/**
 * Real sheet stock typed `purchased`. Sheet-like by text, but NOT raw stock, so
 * it sits behind the toggle rather than being excluded — this is exactly why
 * the escape hatch is not optional.
 */
const PURCHASED_SHEET = part({
  id: 42,
  part_number: 'SHT-304-125',
  name: '304 SS 0.125 Sheet',
  part_type: 'purchased',
});

/**
 * The false positive that motivated the second tier: it passes `isSheetLikePart`
 * on the word "Sheet" and is a box of screws.
 */
const SHEET_METAL_SCREW = part({
  id: 43,
  part_number: 'HW-SMS-8',
  name: 'Sheet metal screw #8',
  part_type: 'hardware',
});

/** A bought component with no sheet words at all. */
const PURCHASED_BOLT = part({
  id: 44,
  part_number: 'HW-BOLT-38',
  name: '3/8-16 x 1 hex bolt',
  part_type: 'purchased',
});

/**
 * A part the shop PRODUCES that also reads as sheet-like. `/materials` cannot
 * serve one today; the picker excludes it anyway, because the prop is typed
 * `Part[]` and the exclusion is the one rule with no escape hatch.
 */
const MANUFACTURED_BRACKET = part({
  id: 45,
  part_number: 'BRK-100',
  name: 'Bracket, sheet metal',
  part_type: 'manufactured',
});

const ASSEMBLY_WELDMENT = part({
  id: 46,
  part_number: 'ASY-200',
  name: 'Weldment, plate assembly',
  part_type: 'assembly',
});

const LABEL = 'Sheet part';

/**
 * Controlled harness — the picker takes `value`/`onChange`, and a pick has to
 * actually stick for the pinning assertions to mean anything.
 */
function Harness({
  parts,
  initialValue = '',
  extraOptions,
}: {
  parts: Part[];
  initialValue?: string;
  extraOptions?: ComboBoxOption[];
}) {
  const [value, setValue] = useState(initialValue);
  return (
    <SheetPartPicker
      parts={parts}
      onHandByPart={null}
      extraOptions={extraOptions}
      value={value}
      onChange={setValue}
      ariaLabel={LABEL}
    />
  );
}

const renderPicker = (props: React.ComponentProps<typeof Harness>) => {
  render(<Harness {...props} />);
  return screen.getByLabelText(LABEL);
};

/** Accessible names of every currently-listed option, minus the empty row. */
const listedNames = (picker: HTMLElement): string[] =>
  comboBoxOptions(picker)
    .map((option) => option.getAttribute('aria-label') ?? option.textContent ?? '')
    .filter((name) => !name.includes('none — untied'));

/**
 * The popup, not the listbox: the filter toggle lives in `<ComboBox>`'s pinned
 * FOOTER, which is a sibling of the `role="listbox"` element inside the portaled
 * popup. Reaching it through the listbox's parent is what keeps these
 * assertions on the real rendered control rather than a test id.
 */
const comboBoxPopup = (picker: HTMLElement): HTMLElement => comboBoxListbox(picker).parentElement as HTMLElement;

/** The "Show all materials (N more)" toggle, and the N it advertises. */
function showAllToggle(picker: HTMLElement): { button: HTMLElement; count: number } {
  const button = within(comboBoxPopup(picker)).getByRole('button', {
    name: /^Show all materials \(\d+ more\)$/,
  });
  const count = Number(/\((\d+) more\)/.exec(button.textContent ?? '')?.[1]);
  return { button, count };
}

const ALL_PARTS = [
  RAW_PLATE,
  PURCHASED_SHEET,
  SHEET_METAL_SCREW,
  PURCHASED_BOLT,
  MANUFACTURED_BRACKET,
  ASSEMBLY_WELDMENT,
];

describe('the default tier is raw stock that is also sheet-like', () => {
  it('lists a raw-material plate under "Sheet & plate"', () => {
    const picker = renderPicker({ parts: ALL_PARTS });
    const list = openComboBox(picker);

    expect(within(list).getByRole('option', { name: /0\.188-72X144-A36/ })).toBeInTheDocument();
    expect(within(list).getByText('Sheet & plate')).toBeInTheDocument();
  });

  it('keeps a purchased bolt and a hardware "Sheet metal screw" out of it', () => {
    const picker = renderPicker({ parts: ALL_PARTS });
    const list = openComboBox(picker);

    // The screw is the case the sheet heuristic alone got wrong: its NAME says
    // "Sheet", so before the raw-stock tier it rendered in the default view of a
    // picker whose field is labelled "Sheet part".
    expect(within(list).queryByRole('option', { name: /HW-SMS-8/ })).toBeNull();
    expect(within(list).queryByRole('option', { name: /HW-BOLT-38/ })).toBeNull();
    // Real sheet that happens to be typed `purchased` is hidden by default too —
    // hence the escape hatch below.
    expect(within(list).queryByRole('option', { name: /SHT-304-125/ })).toBeNull();

    expect(listedNames(picker)).toEqual([expect.stringContaining('0.188-72X144-A36')]);
  });

  it('reveals exactly the rows its count advertises, under "Other materials"', () => {
    const picker = renderPicker({ parts: ALL_PARTS });
    openComboBox(picker);

    const before = listedNames(picker).length;
    const { button, count } = showAllToggle(picker);
    // Purchased sheet, sheet-metal screw, bolt — the two production parts are
    // NOT in this number.
    expect(count).toBe(3);

    fireEvent.click(button);

    const after = listedNames(picker);
    // The count is a promise about what the click does; assert the delta, not a
    // literal, so a count that includes a row already on screen fails here.
    expect(after).toHaveLength(before + count);
    expect(within(comboBoxListbox(picker)).getByText('Other materials')).toBeInTheDocument();
    for (const number of ['SHT-304-125', 'HW-SMS-8', 'HW-BOLT-38']) {
      expect(within(comboBoxListbox(picker)).getByRole('option', { name: new RegExp(number) })).toBeInTheDocument();
    }
  });

  it('offers no toggle at all when there is nothing behind it', () => {
    const picker = renderPicker({ parts: [RAW_PLATE] });
    openComboBox(picker);

    expect(screen.queryByRole('button', { name: /Show all materials/ })).toBeNull();
  });
});

describe('a part the shop PRODUCES is never offered, at either tier', () => {
  it('is absent from the default view and from the widened one', () => {
    const picker = renderPicker({ parts: ALL_PARTS });
    openComboBox(picker);

    expect(within(comboBoxListbox(picker)).queryByRole('option', { name: /BRK-100/ })).toBeNull();
    expect(within(comboBoxListbox(picker)).queryByRole('option', { name: /ASY-200/ })).toBeNull();

    fireEvent.click(showAllToggle(picker).button);

    // The toggle widens the DEFAULT, it does not lift the exclusion. Both the
    // manufactured part and the assembly stay gone.
    expect(within(comboBoxListbox(picker)).queryByRole('option', { name: /BRK-100/ })).toBeNull();
    expect(within(comboBoxListbox(picker)).queryByRole('option', { name: /ASY-200/ })).toBeNull();
  });

  it('is not counted as something the toggle would reveal', () => {
    // A count of 5 here would promise two rows the click can never produce.
    const picker = renderPicker({ parts: ALL_PARTS });
    openComboBox(picker);

    expect(showAllToggle(picker).count).toBe(3);
  });

  it('is not pinned back in even when it is the current selection', () => {
    // A legacy tie to a produced part. Surfacing it as an option — even a
    // pinned one — would make the bad tie re-selectable, which is the opposite
    // of closing the path.
    const picker = renderPicker({ parts: ALL_PARTS, initialValue: String(MANUFACTURED_BRACKET.id) });
    openComboBox(picker);

    expect(within(comboBoxListbox(picker)).queryByRole('option', { name: /BRK-100/ })).toBeNull();
    fireEvent.click(showAllToggle(picker).button);
    expect(within(comboBoxListbox(picker)).queryByRole('option', { name: /BRK-100/ })).toBeNull();
  });

  it('leaves the picker usable — the remaining material is still pickable', () => {
    const picker = renderPicker({ parts: [MANUFACTURED_BRACKET, RAW_PLATE] });
    const list = openComboBox(picker);

    fireEvent.click(within(list).getByRole('option', { name: /0\.188-72X144-A36/ }));
    expect(picker).toHaveValue('0.188-72X144-A36 — A36 HR plate');
  });
});

describe('the current selection survives the filter (anti-silent-untie)', () => {
  it('pins a purchased sheet the default tier would hide, and marks it selected', () => {
    const picker = renderPicker({ parts: ALL_PARTS, initialValue: String(PURCHASED_SHEET.id) });

    expect(picker).toHaveValue('SHT-304-125 — 304 SS 0.125 Sheet');

    const list = openComboBox(picker);
    const option = within(list).getByRole('option', { name: /SHT-304-125/ });
    expect(option).toHaveAttribute('aria-selected', 'true');
  });

  it('does not count the pinned row as something still hidden', () => {
    // It is already on screen; counting it advertises "one more" that the click
    // cannot produce.
    const picker = renderPicker({ parts: ALL_PARTS, initialValue: String(PURCHASED_SHEET.id) });
    openComboBox(picker);

    const { count } = showAllToggle(picker);
    expect(count).toBe(2);

    const before = listedNames(picker).length;
    fireEvent.click(showAllToggle(picker).button);
    expect(listedNames(picker)).toHaveLength(before + count);
  });

  it('keeps a pick made through the escape hatch listed after the toggle flips back', () => {
    const picker = renderPicker({ parts: ALL_PARTS });
    openComboBox(picker);
    fireEvent.click(showAllToggle(picker).button);
    fireEvent.click(within(comboBoxListbox(picker)).getByRole('option', { name: /HW-SMS-8/ }));

    // The planner deliberately widened the list and picked. Narrowing it again
    // must not blank what they chose.
    expect(picker).toHaveValue('HW-SMS-8 — Sheet metal screw #8');
    openComboBox(picker);
    fireEvent.click(within(comboBoxPopup(picker)).getByRole('button', { name: 'Show sheet & plate only' }));

    // Narrowed again, and the pick is still there — pinned, and still marked as
    // the committed option rather than merely echoed in the trigger.
    expect(within(comboBoxListbox(picker)).getByRole('option', { name: /HW-SMS-8/ })).toHaveAttribute(
      'aria-selected',
      'true'
    );
    // The trigger shows the search query while the popup is open, so close it
    // before reading the committed label back.
    fireEvent.keyDown(picker, { key: 'Tab' });
    expect(picker).toHaveValue('HW-SMS-8 — Sheet metal screw #8');
  });

  it('still lists a tie to a part the material load never returned', () => {
    // Capped list, deactivated part, or a failed /materials read: the option is
    // appended by the caller and must not be double-counted either.
    const picker = renderPicker({
      parts: ALL_PARTS,
      initialValue: '99',
      extraOptions: [{ value: '99', label: 'SHT-OLD-1 — Retired 0.075 sheet' }],
    });
    openComboBox(picker);

    expect(within(comboBoxListbox(picker)).getByRole('option', { name: /SHT-OLD-1/ })).toBeInTheDocument();
    expect(showAllToggle(picker).count).toBe(3);
  });
});
