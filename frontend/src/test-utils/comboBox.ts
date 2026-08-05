/**
 * Driving a `<ComboBox>` (and anything wrapping it, e.g. `<SheetPartPicker>`)
 * from a test.
 *
 * These pickers used to be native `<select>`s, and a native select is trivially
 * scriptable: `fireEvent.change(el, { target: { value: '31' } })`, then read
 * `el.options`. An ARIA 1.2 combobox is a text input plus a listbox PORTALED to
 * `document.body`, so neither of those works — `HTMLInputElement` has no
 * `.options`, and its `value` is the *label* of the committed selection, not the
 * id.
 *
 * The helpers here are deliberately thin and behavioral: they click/type/press
 * exactly what a planner would, and they find the listbox the way assistive tech
 * does — through the input's own `aria-controls`, never a test id or a DOM walk
 * from the portal root. That is what keeps a test written against them from
 * passing while the wiring an actual screen reader depends on is broken.
 */

import { fireEvent, within } from '@testing-library/react';

/**
 * The listbox a combobox input currently controls.
 *
 * `aria-controls` is only set while the popup is open, so this doubles as the
 * assertion that the control really opened.
 */
export function comboBoxListbox(input: HTMLElement): HTMLElement {
  const listId = input.getAttribute('aria-controls');
  if (!listId) {
    throw new Error(
      `Combobox "${input.getAttribute('aria-label') ?? input.id}" is closed ` +
        '(no aria-controls) — open it before reading its options.'
    );
  }
  const list = document.getElementById(listId);
  if (!list) throw new Error(`Combobox listbox #${listId} is not in the document.`);
  return list;
}

/** Click the trigger to open the popup, and return the listbox. */
export function openComboBox(input: HTMLElement): HTMLElement {
  fireEvent.click(input);
  return comboBoxListbox(input);
}

/** Type into the trigger (opening it if closed) and return the listbox. */
export function typeInComboBox(input: HTMLElement, text: string): HTMLElement {
  fireEvent.change(input, { target: { value: text } });
  return comboBoxListbox(input);
}

/** Every currently-listed option element, in render order. */
export function comboBoxOptions(input: HTMLElement): HTMLElement[] {
  return within(comboBoxListbox(input)).queryAllByRole('option');
}

/**
 * Assert the currently-listed options, in order, by ACCESSIBLE NAME.
 *
 * Accessible name rather than `textContent`: an option renders its label and
 * its hint as two adjacent `<span>`s, so `textContent` glues them into
 * `"…Sheet12 EA on hand"` while what a screen reader announces — and what
 * `getByRole('option', { name })` matches — is `"… Sheet 12 EA on hand"`.
 * Asserting the announced string is the point.
 */
export function expectComboBoxOptions(input: HTMLElement, names: string[]): void {
  const options = comboBoxOptions(input);
  expect(options).toHaveLength(names.length);
  names.forEach((name, index) => expect(options[index]).toHaveAccessibleName(name));
}

/**
 * Open the picker and commit the option whose accessible name matches.
 *
 * `name` is matched the way `getByRole` matches it, so a `RegExp` picks an
 * option by a distinctive fragment (a part number) without restating the
 * on-hand hint that trails it.
 */
export function selectComboBoxOption(input: HTMLElement, name: string | RegExp): void {
  const list = openComboBox(input);
  fireEvent.click(within(list).getByRole('option', { name }));
}
